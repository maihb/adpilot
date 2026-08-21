"""日指标的查询。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.daily_metric import DailyMetric, MetricLevel
from adpilot.services import ad_account as ad_account_service
from adpilot.services.exceptions import InvalidDataError

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


#: 客户端一次能问的最长区间。挡的不是恶意，是「把 start 填成 2020-01-01」那种
#: 手滑 —— 那会让一次查询扫出几千行再全序列化出去。90 天覆盖「近三个月」这个
#: 最长的常规诉求。
MAX_CLIENT_RANGE_DAYS = 92


@dataclass(frozen=True, slots=True)
class DaySeriesRow:
    """某个账户某一天的汇总：一行数字，不分层级、不分对象。

    看板的时间线和日报的当日/对照期用的是**同一个类型**，因为它们要的本来就是
    同一件事。分成两个（一个只有花费和转化、一个全字段）的代价是「日报想多显示
    一个点击数」时要去改另一个函数 —— 而那两个函数连查询都是一样的。
    """

    stat_date: date
    spend: Decimal
    impressions: int
    clicks: int
    conversions: Decimal
    revenue: Decimal


@dataclass(frozen=True, slots=True)
class DaySeries:
    """一段时间线，外加读懂它所必需的两样东西。"""

    rows: list[DaySeriesRow]

    #: 账户币种。**金额脱离币种没有意义**，而客户可能同时有 USD 和 CNY 的账户。
    currency: str

    #: 账户时区。`stat_date` 是这个时区下的自然日 —— 日报和看板都必须注明口径，
    #: 否则客户拿他自己后台的数字来对，永远差一截（glossary 的「时间口径」）。
    timezone: str


async def series_for_client(
    session: AsyncSession,
    *,
    client_id: int,
    account_id: int,
    start: date,
    end: date,
) -> DaySeries:
    """客户端看板的时间线：一天一行，闭区间。

    `client_id` 是**必填关键字参数**，且第一件事就是确认账户属于他 —— 想在这条
    路径上写出「查全部客户的指标」是做不到的（设计文档第三节那两层保证的第二层）。

    **不分页也不暴露层级。** 客户要的是一条能画成折线的时间线，而层级是内部概念：
    同一天既有账户级又有系列级时两者不能相加（`LEVEL_PRIORITY`），把这个选择交给
    小程序端，迟早会有人把花费显示成双倍。这里逐天按优先级挑，和
    `totals_on_days` 是同一套规则。

    没有数据的那天**不出现在结果里**，不补零 —— 调用方要能分清「那天花了 0」和
    「那天没导入」。
    """
    if start > end:
        raise InvalidDataError("开始日期不能晚于结束日期")
    if (end - start).days >= MAX_CLIENT_RANGE_DAYS:
        raise InvalidDataError(f"一次最多查 {MAX_CLIENT_RANGE_DAYS} 天")

    account = await ad_account_service.get_for_client(session, account_id, client_id=client_id)

    by_day = await _totals_by_day(
        session,
        account_id=account_id,
        day_filter=DailyMetric.stat_date.between(start, end),
    )
    return DaySeries(
        rows=[by_day[day] for day in sorted(by_day)],
        currency=account.currency,
        timezone=account.timezone,
    )


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
) -> dict[date, DaySeriesRow]:
    """给定的几天各自的汇总；没有数据的那天**不出现在返回值里**。

    不给缺数据的日子补一个零：调用方要能分清「那天花了 0」和「那天没导入」——
    拿后者当基线去算周同比，会得出一个凭空的百分比。日报那边同理，缺对照期就
    整段环比留空，而不是说「上升了 100%」。
    """
    if not days:
        return {}

    return await _totals_by_day(
        session,
        account_id=account_id,
        day_filter=DailyMetric.stat_date.in_(days),
    )


async def days_with_data(
    session: AsyncSession,
    *,
    account_id: int,
    days: Sequence[date],
) -> list[date]:
    """给定的这几天里，哪几天**真有指标行**。

    定时日报用它判「那天的数据到齐了吗」。**不做任何汇总** —— 只问在不在，所以
    这里是 `DISTINCT stat_date` 而不是走 `_totals_by_day`：后者要按层级挑一份、
    要 SUM 五个列，而这个问题一个索引扫描就够了。

    🔴 **「有行」和「花了钱」是两件事。** 暂停投放的那天有指标行、`spend` 是 0，
    那天照样该出日报（客户要看到「昨天按计划停了」）。判「有没有花钱」会让暂停
    期间的日报整段消失，而那正是客户最想看到解释的几天。
    """
    if not days:
        return []

    rows = await session.scalars(
        select(DailyMetric.stat_date)
        .where(DailyMetric.account_id == account_id, DailyMetric.stat_date.in_(days))
        .distinct()
    )
    return list(rows.all())


async def _totals_by_day(
    session: AsyncSession,
    *,
    account_id: int,
    day_filter: ColumnElement[bool],
) -> dict[date, DaySeriesRow]:
    """按天汇总，**逐天**按层级优先级挑一份。

    🔴 挑层级这一步是这个函数存在的全部理由：同一天既有账户级又有系列级数据时
    全加起来就是**双倍花费**。而「逐天挑」不是「全区间挑一次」—— 某天只导了系列
    级、另一天导了账户级是完全正常的，按天各挑各的才不会把其中一天算丢。
    """
    rows = await session.execute(
        select(
            DailyMetric.stat_date,
            DailyMetric.level,
            func.coalesce(func.sum(DailyMetric.spend), 0),
            func.coalesce(func.sum(DailyMetric.impressions), 0),
            func.coalesce(func.sum(DailyMetric.clicks), 0),
            func.coalesce(func.sum(DailyMetric.conversions), 0),
            func.coalesce(func.sum(DailyMetric.revenue), 0),
        )
        .where(DailyMetric.account_id == account_id, day_filter)
        .group_by(DailyMetric.stat_date, DailyMetric.level)
    )

    by_day: dict[date, dict[MetricLevel, DaySeriesRow]] = {}
    for day, level, spend, impressions, clicks, conversions, revenue in rows:
        by_day.setdefault(day, {})[level] = DaySeriesRow(
            stat_date=day,
            spend=Decimal(spend),
            impressions=int(impressions),
            clicks=int(clicks),
            conversions=Decimal(conversions),
            revenue=Decimal(revenue),
        )

    picked: dict[date, DaySeriesRow] = {}
    for day, levels in by_day.items():
        for level in LEVEL_PRIORITY:
            totals = levels.get(level)
            if totals is not None:
                picked[day] = totals
                break
    return picked
