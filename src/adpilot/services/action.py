"""投放操作记录：登记、查询，以及日报要的那个「这一段做了什么」。

这一层只有一件事不显然，就是 `window_bounds` —— 把账户时区下的自然日区间换算成
时刻区间。`daily_metrics` 按自然日存、`actions` 按时刻存，日报要把两者对到同一天
上，换算就必须发生在某个地方；**收口在这一个函数里**是为了让它只有一处可能算错。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.action import Action, ActionKind, ActionSource
from adpilot.models.ad_account import AdAccount
from adpilot.models.daily_metric import MetricLevel
from adpilot.services import ad_account as ad_account_service
from adpilot.services.exceptions import InvalidDataError

log = structlog.get_logger(__name__)


async def record(
    session: AsyncSession,
    *,
    account_id: int,
    kind: ActionKind,
    summary: str,
    reason: str,
    performed_at: datetime,
    level: MetricLevel = MetricLevel.ACCOUNT,
    object_id: str | None = None,
    object_name: str | None = None,
    operator: str | None = None,
) -> Action:
    """登记一次投放调整。账户不存在抛 `NotFoundError`。

    **来源固定是 `MANUAL`**，不让调用方传：`PLATFORM` 那条来源要等接了 Ads API
    才有，而在那之前放一个参数出去，只会让人手工登记时随手标成「平台抓的」——
    于是「这条记录里的 `reason` 可不可信」这个问题再也答不上来。

    `performed_at` 在未来直接拒绝（`InvalidDataError`）。这不是洁癖：日报查的是
    过去某个区间，一条落在未来的记录**永远不会出现在任何一期日报里**，而它安安
    静静地躺在库里，看起来像是记过了。
    """
    account = await ad_account_service.get(session, account_id)

    if performed_at > datetime.now(UTC):
        raise InvalidDataError(f"操作时刻不能在未来：{performed_at.isoformat()}")

    action = Action(
        account_id=account.id,
        kind=kind,
        source=ActionSource.MANUAL,
        level=level,
        object_id=object_id,
        object_name=object_name,
        summary=summary,
        reason=reason,
        performed_at=performed_at,
        operator=operator,
    )
    session.add(action)
    await session.flush()

    log.info(
        "action_recorded",
        account_id=account_id,
        kind=kind.value,
        performed_at=performed_at.isoformat(),
    )
    return action


async def list_page(
    session: AsyncSession,
    *,
    account_id: int,
    page: int,
    page_size: int,
) -> tuple[Sequence[Action], int]:
    """分页列出某账户的操作记录，最近做的在前。

    按 `performed_at` 排而不是 `created_at`：补登记的那几条录入时间更晚，但它们
    描述的是更早发生的事，混在一起看会读不出投放的先后。
    """
    await ad_account_service.get(session, account_id)

    where = Action.account_id == account_id
    total = await session.scalar(select(func.count(Action.id)).where(where))
    rows = await session.scalars(
        select(Action)
        .where(where)
        .order_by(Action.performed_at.desc(), Action.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def list_in_window(
    session: AsyncSession,
    *,
    account: AdAccount,
    start: date,
    end: date,
) -> Sequence[Action]:
    """某账户在 `[start, end]`（账户时区下的自然日，闭区间）里做过的事，**发生
    先后排序**。

    日报「本期做了什么」那一段的唯一数据来源，所以顺序是正序而不是倒序：那段话
    要按投放的先后讲，不是按「最近的排前面」。

    收 `AdAccount` 对象而不是 `account_id`，是因为换算要用它的时区，而调用方
    （日报生成）本来就已经把账户查出来了 —— 再查一次只是多一次往返。
    """
    since, until = window_bounds(account.timezone, start, end)
    rows = await session.scalars(
        select(Action)
        .where(
            Action.account_id == account.id,
            Action.performed_at >= since,
            Action.performed_at < until,
        )
        .order_by(Action.performed_at, Action.id)
    )
    return rows.all()


def window_bounds(timezone: str, start: date, end: date) -> tuple[datetime, datetime]:
    """账户时区下的自然日闭区间 `[start, end]` → 时刻区间 `[since, until)`。

    三件都不显然的事：

    * **出参是半开区间**，右端是 `end` 的次日零点。写成「小于等于 `end` 那天的
      23:59:59」会漏掉那一秒之内发生的操作 —— `timestamptz` 是有小数秒的，而漏
      掉的那条记录不会有任何迹象。
    * **零点按账户时区取**，不是服务器时区。用服务器时区去截，日切点附近的操作
      会整体归到相邻那一天，于是日报里「今天做了什么」写的是昨天的事。这和
      `stat_date` 的口径（glossary）必须是同一个时区，否则指标和操作对不上。
    * **夏令时那两天不特殊处理。** 有些时区在切换日没有 00:00 这个本地时刻，
      Python 会按 `fold` 规则给出一个名义时刻，于是那天的窗口比 24 小时多一或少
      一个小时。这是刻意接受的：广告数据本来就按平台的自然日下发，为一年两天的
      一小时去发明另一套口径，只会让每天的口径都变得难解释。
    """
    tz = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, tzinfo=tz),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz),
    )
