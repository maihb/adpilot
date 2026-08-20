"""认证的路由：换 token 和续 token。

这一层不含任何「谁能看什么」的判断 —— 那是 token 里的 `scope` 和各路由挂的依赖
决定的。这里只做两件事：核对身份、签一张票。
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, status

from adpilot.api.deps import OperatorContextDep, SettingsDep
from adpilot.api.errors import responses
from adpilot.auth.password import verify_operator
from adpilot.auth.token import Scope, issue, renew
from adpilot.schemas.auth import OperatorLoginRequest, TokenResponse

router = APIRouter(tags=["auth"])
log = structlog.get_logger(__name__)


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    operation_id="login",
    responses=responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def login(request: OperatorLoginRequest, settings: SettingsDep) -> TokenResponse:
    """运营登录，换一个 8 小时的 token。

    **这是全系统仅有的两个免认证接口之一**（另一个是用邀请码换客户端 token）。
    """
    # 🔴 argon2 是 CPU 密集的（几十毫秒，故意的）。直接在这个 async 函数里调会
    # 卡住整个事件循环 —— 症状是**全局变慢**，不是登录变慢，而那种慢查起来很贵。
    # 见 conventions.md 的异步一节。
    ok = await asyncio.to_thread(
        verify_operator,
        username=request.username,
        password=request.password,
        expected_username=settings.operator_username,
        password_hash=settings.operator_password_hash.get_secret_value(),
    )
    if not ok:
        # 用户名错和密码错**回同一句话**：区分等于确认「这个用户名是存在的」。
        log.warning("operator_login_failed", username=request.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不对",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_at = issue(
        settings.auth_secret,
        scope=Scope.OPERATOR,
        sub=settings.operator_username,
    )
    log.info("operator_login_succeeded", username=request.username)
    return TokenResponse(token=token, expires_at=expires_at)


@router.post(
    "/auth/refresh",
    response_model=TokenResponse,
    operation_id="refreshToken",
    responses=responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def refresh(context: OperatorContextDep, settings: SettingsDep) -> TokenResponse:
    """拿一个尚未过期的运营 token 换一个新的。

    过期了就换不了，只能重新登录 —— 这不是限制，是「短有效期」这件事本身：
    能拿过期 token 续期的话，有效期等于没有。
    """
    token, expires_at = renew(settings.auth_secret, context.token, scope=Scope.OPERATOR)
    return TokenResponse(token=token, expires_at=expires_at)
