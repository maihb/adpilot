"""日指标的查询。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.daily_metric import DailyMetric, MetricLevel

# 🔴 **把某个账户的指标汇总到「账户维度」时，只用其中一个层级的行，不是全部加起来。**
#
# 同一天可能既有账户级又有广告系列级的数据（导了两份不同层级的报表），全加起来就是
# 双倍花费 —— 余额那边会算出翻倍的日均、可撑天数腰斩，异动那边会算出一个凭空的
# 跳变。两处都是「一条长得跟真的一模一样的假告警」。
#
# 挑法是「取有数据的最高层级」：账户级最准（平台自己汇总的，含未归到系列的花费），
# 没有就退到系列级，以此类推。顺序即优先级。
LEVEL_PRIORITY: tuple[MetricLevel, ...] = (
    MetricLevel.ACCOUNT,
    MetricLevel.CAMPAIGN,
    MetricLevel.ADGROUP,
    MetricLevel.AD,
)


@dataclass(frozen=True, slots=True)
class DayTotals:
    """某个账户某一天的汇总。"""

    spend: Decimal
    conversions: Decimal


@dataclass(frozen=True, slots=True)
class WindowTotals:
    """某个账户一段区间的汇总。

    `days_with_data` 是**真有数据的天数**，不是区间长度 —— 缺数据和没花钱是两回事，
    而把它们混起来的代价写在 `rules/balance.py` 的 `average_daily_spend` 里。
    """

    spend: Decimal
    days_with_data: int


async def list_by_account(
    session: AsyncSession,
    *,
    account_id: int,
    start: date,
    end: date,
    level: MetricLevel | None = None,
    page: int,
    page_size: int,
) -> tuple[Sequence[DailyMetric], int]:
    """按天返回某个账户的归一化指标，返回 (本页, 过滤后总数)。

    `start` / `end` **闭区间**，且是账户时区下的自然日 —— 口径见
    [glossary](../../../docs/business/glossary.md) 的「时间口径」。跨账户汇总时
    不要把不同时区的同一个 `stat_date` 当成同一天直接相加。

    排序是 (日期倒序, 对象 ID)。日期倒序是因为看板默认关心最近几天；带上
    `object_id` 是为了让同一天内的行序稳定 —— 只按日期排的话，offset 分页在同一
    天有很多对象时会漏行或重行。
    """
    filters = [
        DailyMetric.account_id == account_id,
        DailyMetric.stat_date >= start,
        DailyMetric.stat_date <= end,
    ]
    if level is not None:
        filters.append(DailyMetric.level == level)

    total = await session.scalar(select(func.count(DailyMetric.id)).where(*filters))
    rows = await session.scalars(
        select(DailyMetric)
        .where(*filters)
        .order_by(DailyMetric.stat_date.desc(), DailyMetric.object_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def window_totals(
    session: AsyncSession,
    *,
    account_id: int,
    start: date,
    end: date,
) -> WindowTotals:
    """一段区间的总花费与有数据的天数，只取一个层级 —— 理由见 `LEVEL_PRIORITY`。

    一次查询把每个层级的汇总都取回来，再在 Python 里按优先级挑。发四条查询轮流试
    更直白，但那是四个 round trip 换一个只有四行的结果集。
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

    for level in LEVEL_PRIORITY:
        summary = by_level.get(level)
        if summary is not None and summary[1] > 0:
            return WindowTotals(spend=Decimal(summary[0]), days_with_data=int(summary[1]))
    return WindowTotals(spend=Decimal(0), days_with_data=0)


async def totals_on_days(
    session: AsyncSession,
    *,
    account_id: int,
    days: Sequence[date],
) -> dict[date, DayTotals]:
    """给定的几天各自的汇总；没有数据的那天**不出现在返回值里**。

    不给缺数据的日子补一个零：调用方要能分清「那天花了 0」和「那天没导入」——
    拿后者当基线去算周同比，会得出一个凭空的百分比。
    """
    if not days:
        return {}

    rows = await session.execute(
        select(
            DailyMetric.stat_date,
            DailyMetric.level,
            func.coalesce(func.sum(DailyMetric.spend), 0),
            func.coalesce(func.sum(DailyMetric.conversions), 0),
        )
        .where(
            DailyMetric.account_id == account_id,
            DailyMetric.stat_date.in_(days),
        )
        .group_by(DailyMetric.stat_date, DailyMetric.level)
    )

    by_day: dict[date, dict[MetricLevel, DayTotals]] = {}
    for day, level, spend, conversions in rows:
        by_day.setdefault(day, {})[level] = DayTotals(
            spend=Decimal(spend),
            conversions=Decimal(conversions),
        )

    # 层级优先级**逐天**挑，不是全区间挑一次：某天只导了系列级、另一天导了账户级
    # 是完全正常的，按天各挑各的才不会把其中一天算丢。
    picked: dict[date, DayTotals] = {}
    for day, levels in by_day.items():
        for level in LEVEL_PRIORITY:
            totals = levels.get(level)
            if totals is not None:
                picked[day] = totals
                break
    return picked
