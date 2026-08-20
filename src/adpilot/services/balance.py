"""余额快照与余额告警。

这一层的分工要说清楚：**查库在这里，算账在 `rules/balance.py`。** 规则那边是纯
函数、参数化测试覆盖得完；这边负责把「最新余额」和「近 N 天日均消耗」这两个数从
两张表里捞出来，捞的过程里有三个只能在这一层解决的判断，都写在下面的注释里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.ad_account import AdAccount
from adpilot.models.balance import Balance
from adpilot.models.daily_metric import DailyMetric, MetricLevel
from adpilot.rules import balance as balance_rules
from adpilot.services import ad_account as ad_account_service
from adpilot.services.exceptions import ConflictError

log = structlog.get_logger(__name__)

# 🔴 **算账户日均消耗时只用其中一个层级的行，不是全部加起来。**
#
# 同一天可能既有账户级又有广告系列级的数据（导了两份不同层级的报表），全加起来
# 就是双倍花费，于是日均翻倍、可撑天数腰斩 —— 一条凭空冒出来的告警。
#
# 挑法是「取有数据的最高层级」：账户级最准（平台自己汇总的，含未归到系列的花费），
# 没有就退到系列级，以此类推。顺序即优先级。
_LEVEL_PRIORITY: tuple[MetricLevel, ...] = (
    MetricLevel.ACCOUNT,
    MetricLevel.CAMPAIGN,
    MetricLevel.ADGROUP,
    MetricLevel.AD,
)


@dataclass(frozen=True, slots=True)
class BalanceAlert:
    """一个账户此刻的余额状况。`runway.is_alerting` 为真就是要告警的那些。"""

    account_id: int
    account_name: str
    currency: str
    runway: balance_rules.BalanceRunway
    captured_at: datetime
    lookback_from: date
    lookback_to: date
    days_with_data: int


async def record(
    session: AsyncSession,
    *,
    account_id: int,
    available: Decimal,
    captured_at: datetime,
    note: str | None = None,
) -> Balance:
    """录一条余额快照。

    账户不存在抛 `NotFoundError`，同一个 (账户, 时刻) 重复抛 `ConflictError`。

    **币种取账户的，不让调用方传。** 余额和消耗必须是同一种货币才能相除，让接口
    传一个币种进来，早晚会出现「账户是 USD、余额录成 CNY」的一条快照 —— 而算出来
    的可撑天数看起来完全正常。
    """
    account = await ad_account_service.get(session, account_id)

    snapshot = Balance(
        account_id=account_id,
        available=available,
        currency=account.currency,
        captured_at=captured_at,
        note=note,
    )
    session.add(snapshot)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"这个时刻的余额已经录过了：{captured_at.isoformat()}") from exc

    log.info("balance_recorded", account_id=account_id, captured_at=captured_at.isoformat())
    return snapshot


async def list_page(
    session: AsyncSession,
    *,
    account_id: int,
    page: int,
    page_size: int,
) -> tuple[Sequence[Balance], int]:
    """分页列出某账户的余额快照，最新的在前。"""
    await ad_account_service.get(session, account_id)

    where = Balance.account_id == account_id
    total = await session.scalar(select(func.count(Balance.id)).where(where))
    rows = await session.scalars(
        select(Balance)
        .where(where)
        .order_by(Balance.captured_at.desc(), Balance.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def alert_for_account(session: AsyncSession, account_id: int) -> BalanceAlert | None:
    """算一个账户的余额告警；从没录过余额就返回 `None`。

    返回 `None` 而不是造一条「余额 0」的告警：**没录过余额不等于余额是 0**。
    把这两件事混起来，会让每个刚建好的账户立刻冒出一条假告警，而假告警会让人
    对整个告警列表脱敏 —— 那比没有告警更糟。
    """
    account = await ad_account_service.get(session, account_id)
    return await _alert(session, account)


async def alerts(session: AsyncSession, *, only_alerting: bool = True) -> list[BalanceAlert]:
    """扫一遍所有**在投**的账户。

    `is_active=False` 的账户不看：停止合作的客户余额多少都不重要，而它们混在
    告警列表里只会让人学会忽略这个列表。
    """
    accounts = await session.scalars(select(AdAccount).where(AdAccount.is_active.is_(True)))

    found: list[BalanceAlert] = []
    for account in accounts:
        alert = await _alert(session, account)
        if alert is None:
            continue
        if only_alerting and not alert.runway.is_alerting:
            continue
        found.append(alert)

    # 最紧急的排前面。`days_left` 可能是 None（无定义），排到最后 —— 它不是
    # 「很安全」，只是「算不出来」，但它一定不在需要今天处理的那一批里。
    found.sort(key=lambda item: (item.runway.days_left is None, item.runway.days_left or 0))
    log.info("balance_alerts_evaluated", alerting=sum(1 for a in found if a.runway.is_alerting))
    return found


async def _alert(session: AsyncSession, account: AdAccount) -> BalanceAlert | None:
    latest = await _latest_balance(session, account.id)
    if latest is None:
        return None

    start, end = _lookback_window(account)
    total_spend, days_with_data = await _spend_in_window(session, account.id, start, end)

    return BalanceAlert(
        account_id=account.id,
        account_name=account.name,
        currency=latest.currency,
        runway=balance_rules.runway(
            latest.available,
            balance_rules.average_daily_spend(total_spend, days_with_data),
        ),
        captured_at=latest.captured_at,
        lookback_from=start,
        lookback_to=end,
        days_with_data=days_with_data,
    )


async def _latest_balance(session: AsyncSession, account_id: int) -> Balance | None:
    """取最新的一条快照。

    按 `captured_at` 排而不是 `created_at`：人可能今天补录昨天看到的数，那条的
    录入时间更晚、但它描述的是更早的时刻。要回答「现在还剩多少」，信的是快照
    自己说的时刻。
    """
    rows = await session.scalars(
        select(Balance)
        .where(Balance.account_id == account_id)
        .order_by(Balance.captured_at.desc(), Balance.id.desc())
        .limit(1)
    )
    return rows.first()


def _lookback_window(account: AdAccount) -> tuple[date, date]:
    """回看窗口：账户时区下的「昨天」往前数 N 天，闭区间。

    两个决定都不显然：

    * **按账户时区取「今天」**，不是服务器本地时区。`stat_date` 的口径就是账户
      时区下的自然日（glossary 的「时间口径」），用服务器时区去截会在日切点附近
      整体差一天，而差一天的日均在周末前后能差出好几成。
    * **排除今天。** 今天的数据还没跑完（而且多半还没导），算进去会把日均拉低
      —— 又是「该告警的不告警」那个危险方向。
    """
    today = datetime.now(ZoneInfo(account.timezone)).date()
    end = today - timedelta(days=1)
    return end - timedelta(days=balance_rules.LOOKBACK_DAYS - 1), end


async def _spend_in_window(
    session: AsyncSession,
    account_id: int,
    start: date,
    end: date,
) -> tuple[Decimal, int]:
    """返回 (窗口内总花费, 有数据的天数)，只取一个层级的行 —— 理由见 `_LEVEL_PRIORITY`。

    一次查询把每个层级的汇总都取回来，再在 Python 里按优先级挑。发四条查询轮流
    试更直白，但那是四个 round trip 换一个只有四行的结果集。
    """
    rows = await session.execute(
        select(
            DailyMetric.level,
            func.coalesce(func.sum(DailyMetric.spend), 0),
            func.count(func.distinct(DailyMetric.stat_date)),
        )
        .where(
            DailyMetric.account_id == account_id,
            DailyMetric.stat_date.between(start, end),
        )
        .group_by(DailyMetric.level)
    )
    by_level = {level: (spend, days) for level, spend, days in rows}

    for level in _LEVEL_PRIORITY:
        summary = by_level.get(level)
        if summary is not None and summary[1] > 0:
            return Decimal(summary[0]), int(summary[1])
    return Decimal(0), 0
