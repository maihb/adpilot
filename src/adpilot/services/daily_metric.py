"""日指标的查询。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.daily_metric import DailyMetric, MetricLevel


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
