"""认证的路由：换 token 和续 token。

这一层不含任何「谁能看什么」的判断 —— 那是 token 里的 `scope` 和各路由挂的依赖
决定的。这里只做两件事：核对身份、签一张票。
"""

from __future__ import annotations

import asyncio
import base64

import structlog
from fastapi import APIRouter, HTTPException, status

from adpilot.api.deps import (
    ClientContextDep,
    ClientScopeDep,
    OperatorContextDep,
    RedisDep,
    SessionDep,
    SettingsDep,
)
from adpilot.api.errors import responses
from adpilot.auth.password import verify_operator
from adpilot.auth.token import Scope, issue, renew
from adpilot.schemas.auth import (
    CaptchaResponse,
    ClientTokenResponse,
    InviteRedeemRequest,
    OperatorLoginRequest,
    TokenResponse,
)
from adpilot.services import invite as invite_service
from adpilot.services import login_guard

router = APIRouter(tags=["auth"])
log = structlog.get_logger(__name__)


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    operation_id="login",
    responses=responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def login(
    request: OperatorLoginRequest, settings: SettingsDep, redis: RedisDep
) -> TokenResponse:
    """运营登录，换一个 8 小时的 token。

    **这是全系统仅有的两个免认证接口之一**（另一个是用邀请码换客户端 token），
    而公网那套实例摘掉 nginx basic auth 之后，它是唯一的防线 —— 所以连续失败两次
    之后要带验证码，见 [登录验证码](../../../docs/design/2026-08-25-login-captcha.md)。
    """
    # 🔴🔴 **验证码必须验在 argon2 之前，这个顺序就是它的全部意义。**
    #
    # 反过来写（先验密码、错了再看验证码）不会有任何测试变红，但验证码就白加了：
    # 攻击者拿一个空的 captcha_answer 照样能驱动服务端做无限次 argon2，只是拿不到
    # 成功那次的响应而已 —— 爆破速率一点没降。这与 auth/token.py 里「验签必须排在
    # 解析和过期判定之前」是同一类要求：错了不报错，只是悄悄失效。
    if await login_guard.captcha_required(redis, username=request.username):
        passed = await login_guard.consume_captcha(
            redis,
            captcha_id=request.captcha_id or "",
            answer=request.captcha_answer or "",
        )
        if not passed:
            log.warning("operator_login_captcha_failed", username=request.username)
            # 计数照样 +1：不然「验证码随便填」就成了一条不涨计数的免费重试通道。
            await login_guard.record_failure(redis, username=request.username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="验证码不对或已过期",
                headers={"WWW-Authenticate": "Bearer"},
            )

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
        await login_guard.record_failure(redis, username=request.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不对",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await login_guard.clear_failures(redis, username=request.username)
    token, expires_at = issue(
        settings.auth_secret,
        scope=Scope.OPERATOR,
        sub=settings.operator_username,
    )
    log.info("operator_login_succeeded", username=request.username)
    return TokenResponse(token=token, expires_at=expires_at)


@router.get(
    "/auth/captcha",
    response_model=CaptchaResponse,
    operation_id="getLoginCaptcha",
    responses=responses(status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def get_login_captcha(username: str, redis: RedisDep) -> CaptchaResponse:
    """这个账号现在要不要验证码；要的话顺带出一张。

    **这是第三个免认证接口**（`tests/test_auth_guard.py` 的豁免清单里有它）——
    它必须免认证，因为调用它的人正是还没登录的那个。

    泄露的信息只有「某个用户名最近连续失败到阈值没有」。运营账号从环境变量来、
    全系统只有一个，这条信息不构成用户名枚举。
    """
    if not await login_guard.captcha_required(redis, username=username):
        return CaptchaResponse(required=False)

    captcha_id, svg = await login_guard.issue_captcha(redis)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return CaptchaResponse(
        required=True,
        captcha_id=captcha_id,
        image=f"data:image/svg+xml;base64,{encoded}",
    )


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


@router.post(
    "/auth/redeem",
    response_model=ClientTokenResponse,
    operation_id="redeemInvite",
    responses=responses(status.HTTP_404_NOT_FOUND, status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def redeem(
    request: InviteRedeemRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> ClientTokenResponse:
    """客户扫码进来：用邀请码换一个 7 天的客户端 token。

    **这是另一个免认证接口。** 码无效、过期、被作废、客户已停止合作，四种情况
    回同一个 404 —— 分开报错等于告诉试码的人「这个码是真的，只是过期了」。

    换出去的 token 之后可以一直续（滑动过期），但从首次签发起最多 90 天。
    """
    client = await invite_service.redeem(session, request.code)

    token, expires_at = issue(settings.auth_secret, scope=Scope.CLIENT, sub=str(client.id))
    log.info("invite_redeemed", client_id=client.id)
    return ClientTokenResponse(token=token, expires_at=expires_at, client_name=client.name)


@router.post(
    "/auth/client/refresh",
    response_model=TokenResponse,
    operation_id="refreshClientToken",
    responses=responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_503_SERVICE_UNAVAILABLE),
)
async def refresh_client(
    context: ClientContextDep,
    client_id: ClientScopeDep,
    settings: SettingsDep,
) -> TokenResponse:
    """客户端续期：常看的人永远不用重扫。

    `client_id` 这个参数看着没用上，它在的理由是**副作用**：`ClientScopeDep` 会
    确认这个客户仍在合作，停止合作的客户续不了期。少了它，一张已经发出去的 token
    可以靠续期一直活到 90 天上限。

    新的到期时间是 `min(现在 + 7 天, 首次签发 + 90 天)`。到了上限就只能重新扫码
    —— 那是这个方案里唯一的强制重新认证时刻，也是「客户换了手机、旧手机被卖掉」
    唯一的兜底。
    """
    token, expires_at = renew(settings.auth_secret, context.token, scope=Scope.CLIENT)
    return TokenResponse(token=token, expires_at=expires_at)
