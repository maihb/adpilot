"""余额快照与余额告警的路由。

三个接口挂同一个 tag：录余额存在的**唯一目的**就是算告警（glossary 里 `balance`
的定义就是「余额告警的依据」），拆成两个领域会让人以为它们能各用各的。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.balance import (
    BalanceAlertListResponse,
    BalanceCreateRequest,
    BalanceItem,
    BalanceListResponse,
    BalanceRunwayResponse,
)
from adpilot.services import balance as balance_service

router = APIRouter(tags=["balances"])


@router.post(
    "/ad-accounts/{account_id}/balances",
    response_model=BalanceItem,
    status_code=status.HTTP_201_CREATED,
    operation_id="recordBalance",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def record_balance(
    account_id: int,
    payload: BalanceCreateRequest,
    session: SessionDep,
) -> BalanceItem:
    """录一条余额快照。

    **手工录入是 MVP 的唯一入口**：预充余额在平台后台的账户页，导出的报表里没有
    这一列，而 MVP 不接 Ads API（理由见设计文档第四节）。接了 API 之后这里会降级
    成补充手段，接口形状不变。

    只增不改：同一个账户录第二条就是第二行，不覆盖旧的 —— 「上周五还剩多少」是个
    会被回头追问的问题。同一个 (账户, 时刻) 重复录返回 409。
    """
    snapshot = await balance_service.record(
        session,
        account_id=account_id,
        available=payload.available,
        captured_at=payload.captured_at,
        note=payload.note,
    )
    return BalanceItem.model_validate(snapshot)


@router.get(
    "/ad-accounts/{account_id}/balances",
    response_model=BalanceListResponse,
    operation_id="listBalances",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_balances(
    account_id: int,
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> BalanceListResponse:
    """列出某账户的余额快照，最新的在前。"""
    rows, total = await balance_service.list_page(
        session,
        account_id=account_id,
        page=page,
        page_size=page_size,
    )
    return BalanceListResponse(
        items=[BalanceItem.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/ad-accounts/{account_id}/balance-runway",
    response_model=BalanceRunwayResponse | None,
    operation_id="getBalanceRunway",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_balance_runway(account_id: int, session: SessionDep) -> BalanceRunwayResponse | None:
    """这个账户的余额还能撑几天。

    ⚠️ **从没录过余额时返回 `null`，不是 404 也不是「0 天」。** 账户是存在的
    （不存在才 404），只是没有余额数据 —— 而「没录过」和「余额是 0」差得远：把
    它们混起来会让每个刚建好的账户立刻冒出一条假告警。

    `days_left` 本身也可能是 `null`：近期没花钱时这个除法没有意义，此时
    `is_alerting` 一定是 false。口径见 `docs/business/balances.md`。
    """
    alert = await balance_service.alert_for_account(session, account_id)
    return _to_response(alert) if alert is not None else None


@router.get(
    "/alerts/balances",
    response_model=BalanceAlertListResponse,
    operation_id="listBalanceAlerts",
)
async def list_balance_alerts(
    session: SessionDep,
    only_alerting: Annotated[
        bool,
        Query(description="只看已触发告警的；关掉就是所有录过余额的在投账户"),
    ] = True,
) -> BalanceAlertListResponse:
    """扫一遍所有**在投**账户的余额，最紧急的排前面。

    停投的账户（`is_active=false`）不看：它们混在列表里只会让人学会忽略这个列表。
    从没录过余额的账户也不出现 —— 那是「不知道」，不是「没事」。

    **没有分页**：这是一张给人当天处理用的清单，长到需要翻页就说明该先去补余额
    数据，而不是往后翻。
    """
    found = await balance_service.alerts(session, only_alerting=only_alerting)
    return BalanceAlertListResponse(
        items=[_to_response(alert) for alert in found],
        total=len(found),
    )


def _to_response(alert: balance_service.BalanceAlert) -> BalanceRunwayResponse:
    """把服务层的结果摊平成出参。

    摊平而不是嵌套一层 `runway`：这个接口的调用方要的是一行能直接显示的东西，
    多一层嵌套只会让前端多写一个 `.runway.`。
    """
    return BalanceRunwayResponse(
        account_id=alert.account_id,
        account_name=alert.account_name,
        currency=alert.currency,
        available=alert.runway.available,
        avg_daily_spend=alert.runway.avg_daily_spend,
        days_left=alert.runway.days_left,
        is_alerting=alert.runway.is_alerting,
        threshold_days=alert.runway.threshold_days,
        captured_at=alert.captured_at,
        lookback_from=alert.lookback_from,
        lookback_to=alert.lookback_to,
        days_with_data=alert.days_with_data,
    )
