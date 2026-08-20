"""文件导入：把外部报表变成 Mongo 里的原始快照。

**这一步不做任何字段映射。** 落进 `raw_reports` 的是未经解释的原始行；归一化是
下一步、单独一次单向转换 —— 映射规则改了或者发现 bug，拿这些快照重跑就是了。
这正是双库边界存在的理由（`db/mongo.py` 的模块 docstring 是那条边界的真相源）。

快照落好之后**排一个归一化任务**（D6 起），接口不等它跑完。排不上队也不让导入
失败 —— 理由写在 `services/task.py` 的 `enqueue_normalize` 里。

🔴 **`raw_reports` append-only**：只 insert，永不 update、永不 delete（CLAUDE.md
硬规矩 4）。同一个 (账户, 日期) 导两次就是两条快照，这是**刻意的** —— 平台数据
在若干天内还会变（归因回传、无效流量剔除、汇率重算），而「当时这个数到底是多少」
只能从这里查。去重是归一化那一步按唯一键 upsert 的事，不是这里的事。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime

import structlog
from celery import Celery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.db.mongo import RAW_REPORTS, MongoDatabase
from adpilot.models.ad_account import AdAccount
from adpilot.models.daily_metric import MetricLevel
from adpilot.providers import registry
from adpilot.providers.base import ParseError
from adpilot.services import task as task_service
from adpilot.services.exceptions import InvalidDataError, ReferenceNotFoundError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """一次导入的结果。**不含快照内容本身** —— 那可能是几千行。"""

    provider: str
    account_id: int
    level: MetricLevel
    days: list[date]
    rows: int
    skipped_rows: int

    #: 排队等着跑的归一化任务。`None` 表示**没排上队**（broker 连不上），快照
    #: 已经落了，归一化得另外触发一次。
    task_id: str | None


async def import_report_file(
    session: AsyncSession,
    mongo: MongoDatabase,
    celery: Celery,
    *,
    account_id: int,
    provider_name: str,
    content: bytes,
    level: MetricLevel,
    date_column: str | None = None,
) -> ImportSummary:
    """解析一份报表文件，按天落成原始快照，并排一个归一化任务。

    账户不存在抛 `ReferenceNotFoundError`，文件解析不了抛 `InvalidDataError`。

    🔴 **`level` 是导入时必须给的元数据，不从文件内容推断。** 后台导出「广告系列」
    层级和「广告」层级，列名不同但都是合法 CSV，推断有很大的猜错空间；而 `level`
    是 `daily_metrics` 唯一键的一部分 —— 猜错了不会覆盖旧行，而是**新增一份**，
    于是同一天的花费在汇总时被算了两遍。它记进快照而不是等归一化时再传，是为了
    重跑时不必有人再记一遍当初导的是什么层级。
    """
    await _ensure_account_exists(session, account_id)

    try:
        provider = registry.create(provider_name, date_column=date_column)
        # 解析是 CPU 密集的（几千行的 CSV），丢进线程池 —— 在事件循环里做会把
        # **整个进程**的所有请求一起卡住，而那种「全局变慢」排查起来非常贵。
        #
        # D6 **没有**把这一步挪进 Celery（原计划如此，实测下来不划算）：文件内容
        # 得跟着消息走一遍 broker，十兆的 base64 塞进 RabbitMQ 是在拿队列当对象
        # 存储用；更要紧的是，解析失败恰恰是**要当场说**的那类错误 —— 层级填错、
        # 日期列认不出来，得趁上传的人还盯着屏幕告诉他。挪进 worker 的是归一化：
        # 那一段重、且没人需要盯着看。
        result = await asyncio.to_thread(provider.parse, content)
    except ParseError as exc:
        raise InvalidDataError(exc.message) from exc

    fetched_at = datetime.now(UTC)
    documents = [
        {
            "provider": provider.name,
            "account_id": account_id,
            # 存字符串而不是枚举成员：BSON 不认识 Python 枚举，而这份文档要能被
            # 任何工具（mongosh、导出脚本）直接读懂。
            "level": level.value,
            # stat_date 存 ISO 字符串而**不是** BSON datetime。BSON 没有「纯日期」
            # 类型，存 datetime 就必须挑一个时刻，而 stat_date 是账户时区下的
            # 自然日、不是时刻 —— 挑 UTC 午夜会诱导下游拿时区去解释它，那正是
            # 这个项目最容易错的地方（glossary 的「时间口径」一节）。
            # ISO 字符串的字典序等于日期序，范围查询照样做得了。
            "stat_date": day.stat_date.isoformat(),
            "fetched_at": fetched_at,
            "payload": day.rows,
        }
        for day in result.days
    ]
    await mongo[RAW_REPORTS].insert_many(documents)

    # 排队而不是当场跑：归一化要读回全部快照、映射几千行、再 upsert 进 PG，把它
    # 留在请求线程里就是让上传的人对着转圈等。**不带 stat_date** —— 一份文件常常
    # 横跨多天，投一个「整个账户重跑」比按天投 N 条省事，而归一化本来就是幂等的。
    task_id = await task_service.enqueue_normalize(celery, account_id=account_id)

    summary = ImportSummary(
        provider=provider.name,
        account_id=account_id,
        level=level,
        days=[day.stat_date for day in result.days],
        rows=sum(len(day.rows) for day in result.days),
        skipped_rows=result.skipped_rows,
        task_id=task_id,
    )
    log.info(
        "raw_report_imported",
        provider=summary.provider,
        account_id=account_id,
        level=level.value,
        days=len(summary.days),
        rows=summary.rows,
        skipped_rows=summary.skipped_rows,
        task_id=task_id,
    )
    return summary


async def _ensure_account_exists(session: AsyncSession, account_id: int) -> None:
    """账户不在就抛 422 那一族的异常。

    快照的 `account_id` 是它唯一的归属标记，落一条指向不存在账户的快照，等于
    造了一份**永远不会被归一化**的数据 —— 而它看起来跟正常快照毫无区别。
    """
    found = await session.scalar(select(AdAccount.id).where(AdAccount.id == account_id))
    if found is None:
        raise ReferenceNotFoundError(f"广告账户不存在：{account_id}")
