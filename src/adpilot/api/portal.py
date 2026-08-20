"""客户端的路由：客户自己看自己的数据。

**全只读，且作用域锁死在一个客户上。** 每个路由都声明 `ClientScopeDep`，它从
token 解出 `client_id` 并确认这个客户仍在合作；路径上除了 `account_id` 之外没有
任何标识入参，而那个 ID 每次都要经 `services` 确认归属。

### 为什么路径是 `/api/portal/` 而不是 `/api/client/`

设计文档原稿写的是后者。落地时改掉，是因为 `/api/clients`（运营管客户）和
`/api/client/...`（客户看自己）**只差一个 `s`，而两者的授权模型正好相反** ——
将来 review 一段路径字符串时，那个差别小到不足以让人停下来。`portal`（客户自助
门户）让模块名、tag 和路径三者一致，作用域门禁也按这个前缀扫。

### 为什么不复用内部那几个 handler

`api/` 不写业务判断是硬规矩，而「同一套接口 + 在 handler 里判断身份」正是那条
规矩要挡的东西。代价是有一部分查询要在两边各调一次 `services/` —— **重复的是
调用，不是逻辑**。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import ClientScopeDep, SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.portal import (
    PortalAccountItem,
    PortalAccountListResponse,
    PortalAlertItem,
    PortalAlertListResponse,
    PortalMetricDay,
    PortalMetricsResponse,
    PortalProfileResponse,
    PortalRunwayResponse,
)
from adpilot.services import ad_account as ad_account_service
from adpilot.services import alert as alert_service
from adpilot.services import balance as balance_service
from adpilot.services import client as client_service
from adpilot.services import daily_metric as daily_metric_service

router = APIRouter(prefix="/portal", tags=["portal"])

#: 账户列表的页大小。客户手上的账户是个位数，一次给完，前端不用写分页。
_ALL_ACCOUNTS = 100


@router.get(
    "/me",
    response_model=PortalProfileResponse,
    operation_id="getPortalProfile",
)
async def get_profile(client_id: ClientScopeDep, session: SessionDep) -> PortalProfileResponse:
    """我是谁。小程序第一屏用它显示「XX 的投放看板」。"""
    client = await client_service.get(session, client_id)
    return PortalProfileResponse(id=client.id, name=client.name)


@router.get(
    "/accounts",
    response_model=PortalAccountListResponse,
    operation_id="listPortalAccounts",
)
async def list_accounts(
    client_id: ClientScopeDep,
    session: SessionDep,
) -> PortalAccountListResponse:
    """我的广告账户。停投的也列出来（带 `is_active`）——「为什么这个号没数据了」
    是客户最常问的问题之一，藏起来只会让他来问人。"""
    rows, total = await ad_account_service.list_page(
        session,
        page=1,
        page_size=_ALL_ACCOUNTS,
        client_id=client_id,
    )
    return PortalAccountListResponse(
        items=[PortalAccountItem.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/accounts/{account_id}/daily-metrics",
    response_model=PortalMetricsResponse,
    operation_id="listPortalMetrics",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def list_metrics(
    account_id: int,
    client_id: ClientScopeDep,
    session: SessionDep,
    start: Annotated[date, Query(description="起始日，闭区间，账户时区下的自然日")],
    end: Annotated[date, Query(description="结束日，闭区间")],
) -> PortalMetricsResponse:
    """一个账户的每日时间线，一天一行。

    `stat_date` 是**账户时区**下的自然日，不是客户所在时区 —— 出参里带着
    `timezone` 就是为了让前端能把这句口径显示出来。不注明的话，客户拿他自己
    后台的数字来对，永远差一截（[glossary](../../../docs/business/glossary.md)）。

    不属于自己的 `account_id` 返回 404。
    """
    series = await daily_metric_service.series_for_client(
        session,
        client_id=client_id,
        account_id=account_id,
        start=start,
        end=end,
    )
    return PortalMetricsResponse(
        account_id=account_id,
        currency=series.currency,
        timezone=series.timezone,
        items=[PortalMetricDay.model_validate(row) for row in series.rows],
    )


@router.get(
    "/accounts/{account_id}/balance-runway",
    response_model=PortalRunwayResponse,
    operation_id="getPortalRunway",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_runway(
    account_id: int,
    client_id: ClientScopeDep,
    session: SessionDep,
) -> PortalRunwayResponse:
    """这个账户的余额还能撑几天。

    预充模式下余额归零就是**直接停投**（不是降速），而重开一次要再熬 3–5 天
    学习期 —— 所以这是客户端最该显眼的一个数字。

    从没录过余额时各字段是 `null`：那是「不知道」，不是「没事」。
    """
    alert = await balance_service.runway_for_client(
        session,
        client_id=client_id,
        account_id=account_id,
    )
    if alert is None:
        account = await ad_account_service.get_for_client(session, account_id, client_id=client_id)
        return PortalRunwayResponse(
            account_id=account_id,
            currency=account.currency,
            available=None,
            avg_daily_spend=None,
            days_left=None,
            is_alerting=False,
            threshold_days=None,
            captured_at=None,
            lookback_from=None,
            lookback_to=None,
            days_with_data=None,
        )

    return PortalRunwayResponse(
        account_id=alert.account_id,
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


@router.get(
    "/alerts",
    response_model=PortalAlertListResponse,
    operation_id="listPortalAlerts",
)
async def list_alerts(
    client_id: ClientScopeDep,
    session: SessionDep,
    only_open: Annotated[
        bool,
        Query(description="只看还没解决的；关掉就是全部历史"),
    ] = True,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
) -> PortalAlertListResponse:
    """我这边有什么要注意的。**默认只给未解决的** —— 客户看的是当下，不是台账。

    出参比内部那套少一个 `notified_at`（推送成功没有是运维信息）。
    """
    rows, total = await alert_service.list_for_client(
        session,
        client_id=client_id,
        only_open=only_open,
        page=page,
        page_size=page_size,
    )
    return PortalAlertListResponse(
        items=[PortalAlertItem.model_validate(row) for row in rows],
        total=total,
    )
