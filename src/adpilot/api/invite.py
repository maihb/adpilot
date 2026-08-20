"""邀请码的路由。

挂在客户下面（`/clients/{client_id}/invites`）而不是自成一个顶层资源：一个邀请码
脱离客户没有任何意义，而这个形状让「拿别的客户的 invite_id 来试」天然要在服务层
带上 `client_id` 一起查。tag 也跟着用 `clients`，业务规则写在
[auth.md](../../../docs/business/auth.md)（它是认证链路的一部分）。
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from fastapi import APIRouter, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.schemas.invite import (
    InviteCreatedResponse,
    InviteCreateRequest,
    InviteListResponse,
    InviteResponse,
)
from adpilot.services import invite as invite_service

router = APIRouter(tags=["clients"])
log = structlog.get_logger(__name__)


@router.post(
    "/clients/{client_id}/invites",
    response_model=InviteCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createInvite",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def create_invite(
    client_id: int,
    request: InviteCreateRequest,
    session: SessionDep,
) -> InviteCreatedResponse:
    """给一个客户生成邀请码。

    🔴 **响应里的 `code` 是明文，而且只有这一次**。库里存的是 SHA-256，谁也还原
    不回去 —— 运营要么当场把它渲染成二维码发出去，要么就只能再生成一个。
    """
    invite, code = await invite_service.create(
        session,
        client_id=client_id,
        ttl=timedelta(days=request.ttl_days),
    )
    # 只记 ID 和客户，**不记码本身**（conventions.md 的日志一节：日志里不写凭据）。
    log.info("invite_created", invite_id=invite.id, client_id=client_id)
    return InviteCreatedResponse(**InviteResponse.model_validate(invite).model_dump(), code=code)


@router.get(
    "/clients/{client_id}/invites",
    response_model=InviteListResponse,
    operation_id="listInvites",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_invites(client_id: int, session: SessionDep) -> InviteListResponse:
    """列出一个客户的全部邀请码，新的在前。**不含明文码。**

    没有分页：一个客户身上的码是个位数。
    """
    rows = await invite_service.list_for_client(session, client_id=client_id)
    return InviteListResponse(
        items=[InviteResponse.model_validate(row) for row in rows],
        total=len(rows),
    )


@router.post(
    "/clients/{client_id}/invites/{invite_id}/revoke",
    response_model=InviteResponse,
    operation_id="revokeInvite",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def revoke_invite(client_id: int, invite_id: int, session: SessionDep) -> InviteResponse:
    """作废一个邀请码。

    ⚠️ **已经换出去的 token 不受影响**，它们是自包含的，最多再活 7 天。要立刻
    切断这个客户的全部访问，把客户置成 `is_active=false`。

    用 POST 一个动作而不是 `DELETE /invites/{id}`：作废不是删除，那一行要留着 ——
    「这个码什么时候发的、被用过几次、什么时候断的」是运营唯一能自查的线索。
    """
    invite = await invite_service.revoke(session, client_id=client_id, invite_id=invite_id)
    log.info("invite_revoked", invite_id=invite_id, client_id=client_id)
    return InviteResponse.model_validate(invite)
