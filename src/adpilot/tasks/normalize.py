"""归一化任务：把某账户的原始快照转成 `daily_metrics`。

**编排在这里，逻辑在 `services/normalize.py`。** 这个任务只做四件事：把 broker 送
来的原始类型解回领域类型、开事务、调服务、把结果整理成能进 result backend 的形状。
一旦这里开始出现 `if`，说明那段判断走错了层。

同一条链路上有两个调用方：接口（`POST /api/ad-accounts/{id}/normalize`，同步跑、
当场返回行数）和这个任务（导入完自动排队）。两者调的是同一个服务函数 —— 这正是
「`services/` 不认识 HTTP」那条规矩换来的东西。
"""

from __future__ import annotations

from datetime import date

import structlog
from celery.exceptions import Reject

from adpilot.db.broker import TASK_NORMALIZE_ACCOUNT
from adpilot.db.postgres import transaction
from adpilot.resources import Resources
from adpilot.services import normalize as normalize_service
from adpilot.services.exceptions import DomainError
from adpilot.tasks import runtime
from adpilot.tasks.app import AdpilotTask, celery_app

log = structlog.get_logger(__name__)


@celery_app.task(base=AdpilotTask, bind=True, name=TASK_NORMALIZE_ACCOUNT)
def normalize_account(
    self: AdpilotTask,
    *,
    account_id: int,
    stat_date: str | None = None,
) -> dict[str, object]:
    """把该账户的快照归一化进 `daily_metrics`；`stat_date` 是 ISO 日期或 `None`。

    ⚠️ **参数和返回值只能是 JSON 能表示的类型**（配置里 `task_serializer="json"`）。
    所以日期进出都是 ISO 字符串，`date` 对象在投递那一刻就会让序列化失败 —— 而那个
    报错发生在接口进程里，看起来跟这个任务毫无关系。

    **可以反复跑**：归一化按唯一键 upsert，重跑是覆盖不是追加。所以重试是安全的，
    这也是它敢配 `acks_late` 的前提 —— worker 崩在半路，消息回队列再跑一遍就是了。
    """
    try:
        day = date.fromisoformat(stat_date) if stat_date else None
    except ValueError as exc:
        # 投递方给了个不是日期的字符串。重试一万次还是同一个字符串。
        raise Reject(f"stat_date 不是 ISO 日期：{stat_date!r}", requeue=False) from exc

    try:
        summary = runtime.run(
            lambda resources: _normalize(resources, account_id=account_id, stat_date=day)
        )
    except DomainError as exc:
        # 账户不存在、快照缺必需列 —— 数据本身的问题，重试不会变好。进死信队列
        # 等人来看，别把真正的原因埋进五条一模一样的重试日志里。
        log.error(
            "normalize_task_rejected",
            account_id=account_id,
            stat_date=stat_date,
            task_id=self.request.id,
            reason=exc.message,
        )
        raise Reject(exc.message, requeue=False) from exc

    return {
        "account_id": summary.account_id,
        "days": [day.isoformat() for day in summary.days],
        "rows": summary.rows,
        "snapshots": summary.snapshots,
        "skipped_rows": summary.skipped_rows,
    }


async def _normalize(
    resources: Resources,
    *,
    account_id: int,
    stat_date: date | None,
) -> normalize_service.NormalizeSummary:
    async with transaction(resources.session_factory) as session:
        return await normalize_service.normalize_account(
            session,
            resources.mongo_db,
            account_id=account_id,
            stat_date=stat_date,
        )
