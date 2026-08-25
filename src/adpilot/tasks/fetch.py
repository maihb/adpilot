"""定时拉取任务：把此刻该拉的账户都拉一遍。

编排在这里，判定和拉取在 `services/fetch.py` —— 和另外两个 beat 任务同一个形状。

**这是第三个由 beat 触发的任务**，排在每小时第 40 分（`db/broker.py` 的
`FETCH_SCHEDULE_MINUTE` 讲了为什么是这个数字）。

## 🔴 一个账户失败，别的照拉

和定时日报**相反**的取舍：那个任务撞到额度上限就整轮中断，因为继续只会把剩下的
调用全撞在同一堵墙上。拉取不花钱，而一个账户的 token 失效不该让其它账户跟着停更
——它们各自有各自的凭据。

## 🔴 失败记录要单独一个事务

拉取失败会让那个账户的事务回滚，**连同刚写进 `fetch_states` 的失败记录一起**。
于是「这个账户拉不到数」永远不会被巡检看见，而看板上那片 0 花费看起来一切正常。

所以下面每个账户走两个事务：一个干活，失败时另起一个记账。这是本模块最容易被
「顺手合并一下」改坏的地方，而改坏之后不会有任何报错。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from celery.exceptions import Reject

from adpilot.config import Settings, get_settings
from adpilot.db.broker import TASK_FETCH_DUE_ACCOUNTS
from adpilot.db.postgres import transaction
from adpilot.resources import Resources
from adpilot.services import fetch as fetch_service
from adpilot.services import normalize as normalize_service
from adpilot.services.exceptions import DomainError
from adpilot.tasks import runtime
from adpilot.tasks.app import AdpilotTask, celery_app

log = structlog.get_logger(__name__)


@celery_app.task(base=AdpilotTask, bind=True, name=TASK_FETCH_DUE_ACCOUNTS)
def fetch_due_accounts(self: AdpilotTask) -> dict[str, object]:
    """把每个「该拉但还没拉」的账户拉一遍，顺手归一化。

    **没有参数** —— 输入就是「库里此刻的样子」，同另外两个定时任务。

    可以反复跑：判定的第三个条件是「这个日切之后还没成功拉过」，快照 append-only、
    归一化按唯一键 upsert，所以重跑最多是多一条快照。
    """
    try:
        summary = runtime.run(_fetch_due)
    except DomainError as exc:
        # 走到这里说明**判定阶段**就出了问题（比如某个账户的时区名不存在），
        # 不是某个账户拉取失败 —— 后者在下面被逐个吞掉并计进 `failed`。
        log.error("scheduled_fetch_rejected", task_id=self.request.id, reason=exc.message)
        raise Reject(exc.message, requeue=False) from exc

    return {
        "accounts": summary.accounts,
        "fetched": summary.fetched,
        "failed": summary.failed,
        "rows": summary.rows,
    }


@dataclass(slots=True)
class _Summary:
    """一轮拉取的计数。出了任务体就变成 result backend 里的一个 dict。"""

    accounts: int = 0
    fetched: int = 0
    failed: int = 0
    rows: int = 0


async def _fetch_due(resources: Resources) -> _Summary:
    settings = get_settings()
    summary = _Summary()

    # 判定单独一个事务，**而且要在这里就把账户 ID 取出来**：后面每个账户各开各的
    # 事务，跨事务用一个已经脱离 session 的 ORM 对象会触发懒加载，而那在 async 下
    # 抛的是 MissingGreenlet —— 报错和真正的原因之间毫无线索。
    async with transaction(resources.session_factory) as session:
        due = await fetch_service.due_accounts(session, settings)
        account_ids = [account.id for account in due]

    summary.accounts = len(account_ids)
    for account_id in account_ids:
        rows = await _fetch_one(resources, settings, account_id=account_id)
        if rows is None:
            summary.failed += 1
        else:
            summary.fetched += 1
            summary.rows += rows

    log.info(
        "scheduled_fetch_finished",
        accounts=summary.accounts,
        fetched=summary.fetched,
        failed=summary.failed,
        rows=summary.rows,
    )
    return summary


async def _fetch_one(
    resources: Resources,
    settings: Settings,
    *,
    account_id: int,
) -> int | None:
    """拉一个账户。成功返回行数，失败返回 `None` 并记下这次失败。

    归一化在**同一个事务**里同步跑，不再投一条消息：已经在 worker 里了，排队只会
    让「拉完了但数字还没变」多出一个中间状态，而那个状态没有任何人需要观察。
    """
    try:
        async with transaction(resources.session_factory) as session:
            result = await fetch_service.fetch_account(
                session,
                resources.mongo_db,
                settings,
                account_id=account_id,
            )
            await normalize_service.normalize_account(
                session,
                resources.mongo_db,
                account_id=account_id,
            )
            await fetch_service.record_success(session, account_id=account_id)
            return result.rows
    except DomainError as exc:
        log.warning("account_fetch_failed", account_id=account_id, reason=exc.message)
        # 🔴 **另起一个事务**：上面那个已经回滚了，把失败记在它里面等于没记。
        async with transaction(resources.session_factory) as session:
            await fetch_service.record_failure(session, account_id=account_id, error=exc.message)
        return None
