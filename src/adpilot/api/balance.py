"""余额快照的路由：录进来、查回去、算这个账户还能撑几天。

**跨账户的余额清单不在这里**，它在 `api/alert.py` —— 所有 `/alerts*` 归一处，
不然 OpenAPI 分组时同一个路径前缀会散在两个 tag 下。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.balance import (
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
    return to_runway_response(alert) if alert is not None else None


def to_runway_response(alert: balance_service.BalanceAlert) -> BalanceRunwayResponse:
    """把服务层的结果摊平成出参。

    摊平而不是嵌套一层 `runway`：调用方要的是一行能直接显示的东西，多一层嵌套只会
    让前端多写一个 `.runway.`。

    放在这个模块而不是 `schemas/`，是因为它认识 `services` 里的类型，而分层契约里
    `schemas` 在 `services` **之下** —— 那边够不着。`api/alert.py` 的余额清单也用
    它，两处共一份，免得摊平规则改一处漏一处。
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
