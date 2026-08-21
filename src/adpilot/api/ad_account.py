"""广告账户的路由。

**tag 与 `client.py` 共用 `clients`**，虽然是两个文件。tag 的粒度对齐的是
[业务文档](../../../docs/business/BUSINESS.md)的领域划分，而「客户与账户」在那里
是一个领域 —— 账户脱离客户没有意义，两者的规则也写在同一篇里。文件仍按实体拆，
是为了 `grep ad_account` 能一次找齐 model / schema / service / 路由四处。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.models.ad_account import Platform
from adpilot.schemas.ad_account import (
    AdAccountCreateRequest,
    AdAccountListResponse,
    AdAccountResponse,
    AdAccountUpdateRequest,
)
from adpilot.services import ad_account as ad_account_service

router = APIRouter(tags=["clients"])


@router.post(
    "/ad-accounts",
    response_model=AdAccountResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createAdAccount",
    responses=responses(
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def create_ad_account(
    request: AdAccountCreateRequest,
    session: SessionDep,
) -> AdAccountResponse:
    """建一个广告账户。

    `client_id` 指向不存在的客户返回 422，`(platform, external_id)` 重复返回 409
    —— 后者是幂等导入的依据，重复建会让同一天的数据分裂到两行。
    """
    account = await ad_account_service.create(
        session,
        client_id=request.client_id,
        platform=request.platform,
        external_id=request.external_id,
        name=request.name,
        currency=request.currency,
        timezone=request.timezone,
        auto_report=request.auto_report,
        report_delay_hours=request.report_delay_hours,
    )
    return AdAccountResponse.model_validate(account)


@router.get(
    "/ad-accounts",
    response_model=AdAccountListResponse,
    operation_id="listAdAccounts",
)
async def list_ad_accounts(
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
    client_id: Annotated[int | None, Query(description="只看某个客户的账户")] = None,
    platform: Annotated[Platform | None, Query(description="只看某个平台")] = None,
    is_active: Annotated[bool | None, Query(description="只看在投的 / 已停的")] = None,
) -> AdAccountListResponse:
    """分页列出广告账户，新建的在前。"""
    rows, total = await ad_account_service.list_page(
        session,
        page=page,
        page_size=page_size,
        client_id=client_id,
        platform=platform,
        is_active=is_active,
    )
    return AdAccountListResponse(
        items=[AdAccountResponse.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/ad-accounts/{account_id}",
    response_model=AdAccountResponse,
    operation_id="getAdAccount",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_ad_account(account_id: int, session: SessionDep) -> AdAccountResponse:
    """按 ID 取一个广告账户。"""
    account = await ad_account_service.get(session, account_id)
    return AdAccountResponse.model_validate(account)


@router.patch(
    "/ad-accounts/{account_id}",
    response_model=AdAccountResponse,
    operation_id="updateAdAccount",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def update_ad_account(
    account_id: int,
    request: AdAccountUpdateRequest,
    session: SessionDep,
) -> AdAccountResponse:
    """改账户，只动请求体里出现的字段。

    **`platform` 与 `external_id` 改不了**（请求体里根本没有这两个字段）：它们是
    账户的身份，改了等于换了一个账户，而历史 `daily_metrics` 仍挂在原
    `account_id` 上。真要换就建一个新账户、把旧的停用。
    """
    account = await ad_account_service.update(
        session,
        account_id,
        request.model_dump(exclude_unset=True),
    )
    return AdAccountResponse.model_validate(account)
