"""日指标：归一化与查询。

两个接口挂同一个 tag，因为它们是同一件事的两头 —— 一头把快照变成日指标，另一头
把日指标读出来。规则写在同一篇业务文档里（`docs/business/metrics.md`）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import MongoDep, SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.models.daily_metric import MetricLevel
from adpilot.schemas.daily_metric import (
    DailyMetricItem,
    DailyMetricListResponse,
    NormalizeResponse,
)
from adpilot.services import ad_account as ad_account_service
from adpilot.services import daily_metric as daily_metric_service
from adpilot.services import normalize as normalize_service

router = APIRouter(tags=["metrics"])


@router.post(
    "/ad-accounts/{account_id}/normalize",
    response_model=NormalizeResponse,
    operation_id="normalizeAccount",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def normalize_account(
    account_id: int,
    session: SessionDep,
    mongo: MongoDep,
    stat_date: Annotated[
        date | None,
        Query(description="只跑这一天；不给就跑该账户的全部快照（重跑历史用）"),
    ] = None,
) -> NormalizeResponse:
    """把该账户的原始快照归一化进 `daily_metrics`。

    **可以反复跑。** 同一个 (账户, 层级, 对象, 日期) 按唯一键 upsert，重跑是覆盖
    不是追加。同一天有多条快照时只用 `fetched_at` 最新的那条 —— 平台数据在若干天
    内还会变，最新那条才是当前最准确的说法，旧的留着回答「当时那个数是多少」。

    没有任何快照时返回 0 行而不是报错：那通常意味着还没导入，不是错误。
    """
    summary = await normalize_service.normalize_account(
        session,
        mongo,
        account_id=account_id,
        stat_date=stat_date,
    )
    return NormalizeResponse.model_validate(summary)


@router.get(
    "/ad-accounts/{account_id}/daily-metrics",
    response_model=DailyMetricListResponse,
    operation_id="listDailyMetrics",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_daily_metrics(
    account_id: int,
    session: SessionDep,
    start: Annotated[date, Query(description="起始日，闭区间")],
    end: Annotated[date, Query(description="结束日，闭区间")],
    level: Annotated[MetricLevel | None, Query(description="只看某个投放层级")] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> DailyMetricListResponse:
    """按天返回某账户的归一化指标，附带现算的派生指标。

    `stat_date` 是**广告账户时区下的自然日**（时区记在 `ad_accounts.timezone`），
    口径见 `docs/business/glossary.md`。跨账户汇总时不要把不同时区的同一个
    `stat_date` 当成同一天直接相加 —— 能加，但要知道加的是什么，并在日报里注明。

    CPM / CPC / CTR / CPA / ROAS 都是现算的，**分母为 0 时返回 `null`**，不是 0。

    日期区间必填：不给的话默认全量，而这是唯一一张按天线性增长的表。
    """
    # 先确认账户存在，否则「账户不存在」和「这段时间没有数据」会返回同一个空列表，
    # 而前端分不出该提示「查无此账户」还是「换个日期试试」。
    await ad_account_service.get(session, account_id)

    rows, total = await daily_metric_service.list_by_account(
        session,
        account_id=account_id,
        start=start,
        end=end,
        level=level,
        page=page,
        page_size=page_size,
    )
    return DailyMetricListResponse(
        items=[DailyMetricItem.model_validate(row) for row in rows],
        total=total,
    )
