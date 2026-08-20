"""客户的路由。

这一层只做三件事：解析入参 → 调服务 → 让领域异常自己去 `errors.py` 翻状态码。
**不写业务判断** —— handler 里出现 `if` 套 `if` 就说明那段该在 `services/`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.client import (
    ClientCreateRequest,
    ClientListResponse,
    ClientResponse,
    ClientUpdateRequest,
)
from adpilot.services import client as client_service

router = APIRouter(tags=["clients"])


@router.post(
    "/clients",
    response_model=ClientResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createClient",
    responses=responses(status.HTTP_409_CONFLICT),
)
async def create_client(
    request: ClientCreateRequest,
    session: SessionDep,
) -> ClientResponse:
    """建一个客户。客户名重复返回 409。"""
    client = await client_service.create(session, name=request.name, note=request.note)
    return ClientResponse.model_validate(client)


@router.get(
    "/clients",
    response_model=ClientListResponse,
    operation_id="listClients",
)
async def list_clients(
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
    is_active: Annotated[
        bool | None,
        Query(description="只看在合作的 / 只看已停止的；不传则全都要"),
    ] = None,
) -> ClientListResponse:
    """分页列出客户，新建的在前。`total` 是过滤后的总数，不是本页条数。"""
    rows, total = await client_service.list_page(
        session,
        page=page,
        page_size=page_size,
        is_active=is_active,
    )
    return ClientListResponse(
        items=[ClientResponse.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/clients/{client_id}",
    response_model=ClientResponse,
    operation_id="getClient",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def get_client(client_id: int, session: SessionDep) -> ClientResponse:
    """按 ID 取一个客户。"""
    client = await client_service.get(session, client_id)
    return ClientResponse.model_validate(client)


@router.patch(
    "/clients/{client_id}",
    response_model=ClientResponse,
    operation_id="updateClient",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def update_client(
    client_id: int,
    request: ClientUpdateRequest,
    session: SessionDep,
) -> ClientResponse:
    """改客户，只动请求体里出现的字段。

    **停止合作走 `is_active=false`，这个领域没有 DELETE** —— 历史日报和结算记录
    都挂在客户下面，删了它们就成了孤儿。

    `exclude_unset=True` 是关键：它区分「没传 `note`」和「把 `note` 显式设成
    null」，后者是合法操作。少了这个参数，不传的字段会被当成 null 一起写进去。
    """
    client = await client_service.update(
        session,
        client_id,
        request.model_dump(exclude_unset=True),
    )
    return ClientResponse.model_validate(client)
