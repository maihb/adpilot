"""平台授权凭据：换 token、加解密、按凭据造 provider。

分工：**这一层管「哪个账户用哪把钥匙」，`providers/` 管「拿钥匙去问平台」。**
加解密本身在 `auth/crypto.py`（纯计算，够不着数据库 —— 那是分层契约保证的）。

设计见[自动拉取平台数据](../../../docs/design/2026-08-25-ads-api-fetch.md)第五节。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import structlog
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.auth import crypto
from adpilot.auth import token as token_auth
from adpilot.config import Environment, Settings
from adpilot.models.ad_account import Platform
from adpilot.models.fetch import PlatformCredential
from adpilot.providers import registry, tiktok
from adpilot.providers.base import FetchError, FetchProvider, ParseError
from adpilot.services.exceptions import (
    NotConfiguredError,
    NotFoundError,
    UpstreamError,
)

log = structlog.get_logger(__name__)

#: OAuth `state` 里 `sub` 的前缀。**校验时要比对它** —— 只验签名的话，任何一个
#: 合法的运营 token 都能当 state 用，而那正是 `auth/token.py` 里「拿错门也是
#: 不可信」那条规矩说的情形。
_STATE_SUBJECT_PREFIX = "oauth:"

#: 回调路径。**开发者后台里填的必须和它拼出来的完全一致**，差一个字符平台就
#: 拒绝跳转 —— 而那个报错发生在平台那边，我们这里一行日志都不会有。
CALLBACK_PATH = "/api/oauth/tiktok/callback"


def authorize_url(settings: Settings, *, label: str) -> str:
    """拼出让人去点「同意」的那个地址，`state` 现签一个。

    没配 TikTok 应用抛 `NotConfiguredError`（503，是部署方的问题不是点按钮的人
    的问题）。

    ⚠️ **`label` 编进 `state` 里带一圈再回来**，而不是让回调那边再问一次。回调
    是平台跳转过来的，那一刻没有任何界面可以让人填东西 —— 唯一能传递上下文的
    通道就是 `state`。它被签名保护着，所以带业务信息是安全的（内容可读、但改不动）。
    """
    _require_tiktok(settings)
    state, _ = token_auth.issue(
        settings.auth_secret,
        scope=token_auth.Scope.OPERATOR,
        sub=f"{_STATE_SUBJECT_PREFIX}{Platform.TIKTOK.value}:{label}",
    )
    provider = tiktok.TikTokProvider(
        access_token=SecretStr("unused-for-authorize-url"),
        base_url=settings.tiktok_api_base_url or tiktok.DEFAULT_BASE_URL,
    )
    return provider.authorize_url(
        app_id=settings.tiktok_app_id,
        state=state,
        redirect_uri=redirect_uri(settings),
    )


def redirect_uri(settings: Settings) -> str:
    """回调地址。**必须和开发者后台里登记的一模一样。**

    从配置拼而不是从请求的 Host 头推：Host 是客户端说了算的，用它拼回调地址
    等于让请求方决定 token 往哪送。
    """
    return f"{settings.oauth_redirect_base_url.rstrip('/')}{CALLBACK_PATH}"


def verify_state(settings: Settings, state: str) -> str:
    """校验回调带回来的 `state`，返回当初编进去的 label。不合法抛 `NotFoundError`。

    🔴 **这是回调接口唯一的防护。** 那个路由必须公开（浏览器跳转带不了
    Authorization 头，见设计文档 5.4），于是公网上谁都能打它。没有这一步，
    任何人都能诱导一次「把攻击者的广告账户绑进来」的写操作。

    回 404 而不是 401/403 是有意的：对一个纯浏览器跳转的地址，「这里没有东西」
    比「你的凭证不对」少泄露一点信息，而合法用户永远看不到这个响应 —— 他们的
    `state` 是我们自己几分钟前签的。
    """
    try:
        payload = token_auth.verify(
            settings.auth_secret,
            state,
            scope=token_auth.Scope.OPERATOR,
        )
    except token_auth.InvalidTokenError as exc:
        raise NotFoundError("授权回调的 state 不合法或已过期") from exc

    if not payload.sub.startswith(_STATE_SUBJECT_PREFIX):
        # 签名是真的，只是拿错了门 —— 一个普通的运营登录 token 被当成 state 用。
        raise NotFoundError("授权回调的 state 不是授权流程签发的")

    # `oauth:<platform>:<label>`。label 里可能有冒号，所以按最多两刀切。
    parts = payload.sub.split(":", 2)
    return parts[2] if len(parts) == 3 and parts[2] else "未命名授权"


async def create_from_auth_code(
    session: AsyncSession,
    settings: Settings,
    *,
    auth_code: str,
    label: str,
) -> PlatformCredential:
    """拿回调带回来的 `auth_code` 换 token 并落库（密文）。

    换 token 失败抛 `UpstreamError`（502），没配应用抛 `NotConfiguredError`。

    ⚠️ **不去重。** 同一个 BC 授权两次就是两行 —— 平台每次换出来的 token 都是
    新的，而旧的那把可能还在被别的账户用着。哪一行在用，看 `ad_accounts` 挂在
    谁身上；不用了就停用（`deactivate`），不是删。
    """
    _require_tiktok(settings)

    try:
        access_token, advertiser_ids = await tiktok.exchange_auth_code(
            app_id=settings.tiktok_app_id,
            app_secret=settings.tiktok_app_secret,
            auth_code=auth_code,
            base_url=settings.tiktok_api_base_url or tiktok.DEFAULT_BASE_URL,
        )
    except FetchError as exc:
        raise UpstreamError(exc.message, retryable=exc.retryable) from exc

    credential = PlatformCredential(
        platform=Platform.TIKTOK,
        provider=tiktok.TikTokProvider.name,
        label=label,
        access_token=crypto.encrypt(settings.credentials_secret, access_token),
        scope=None,
        external_account_ids=list(advertiser_ids),
    )
    session.add(credential)
    await session.flush()

    # 🔴 日志里记 ID 和账户数量，**永远不记 token 本身**，也不记密文 —— 密文
    # 进了日志，就等于把「解密所需的一半」放进了一个通常权限更宽松的地方。
    log.info(
        "platform_credential_created",
        credential_id=credential.id,
        platform=credential.platform.value,
        accounts=len(advertiser_ids),
    )
    return credential


async def get(session: AsyncSession, credential_id: int) -> PlatformCredential:
    credential = await session.get(PlatformCredential, credential_id)
    if credential is None:
        raise NotFoundError(f"平台凭据不存在：{credential_id}")
    return credential


async def list_all(session: AsyncSession) -> Sequence[PlatformCredential]:
    """全部凭据，新的在前。**不分页** —— 凭据是按授权来的，个位数。"""
    return (
        await session.scalars(select(PlatformCredential).order_by(PlatformCredential.id.desc()))
    ).all()


async def deactivate(session: AsyncSession, credential_id: int) -> PlatformCredential:
    """停用一个凭据。挂在它上面的账户从此不再被排期扫到。

    **不删行**：删掉之后那些历史快照的来源就成了无头案。同 `Client.is_active`
    ——「停止合作」和「从来没有过」是两件不同的事。
    """
    credential = await get(session, credential_id)
    credential.is_active = False
    await session.flush()
    log.info("platform_credential_deactivated", credential_id=credential_id)
    return credential


def open_provider(settings: Settings, credential: PlatformCredential) -> FetchProvider:
    """解密 token，造出能去问平台的那个 provider。

    解不开抛 `NotConfiguredError`（→ 503）：那**几乎总是**部署方的问题 ——
    `CREDENTIALS_SECRET` 换过了或者没配。回 4xx 会让人去怀疑自己点的按钮。

    ⚠️ 停用的凭据在这里**不拦**。拦在这里会让「先停用、再手动补一次历史数据」
    这种正当操作做不成，而排期任务本来就只挑在用的那些（见 `services/fetch.py`
    的账户筛选）。
    """
    try:
        access_token = crypto.decrypt(settings.credentials_secret, credential.access_token)
    except crypto.CryptoNotConfiguredError as exc:
        raise NotConfiguredError("未配置 CREDENTIALS_SECRET，无法读取已保存的平台凭据") from exc
    except crypto.DecryptionError as exc:
        raise NotConfiguredError(
            f"凭据 {credential.id} 解不开：CREDENTIALS_SECRET 可能换过了，需要重新授权"
        ) from exc

    options = registry.FetchOptions(
        access_token=access_token,
        base_url=settings.tiktok_api_base_url,
        extra_metrics=settings.tiktok_extra_metric_names,
    )
    try:
        return registry.create_fetch(
            credential.provider,
            options,
            # 假 provider 只在非生产环境造得出来。判据留在这一层是因为
            # `providers/` 不认识应用配置（同各 provider「收零件不收 Settings」）。
            allow_fake=settings.environment is not Environment.PROD,
        )
    except ParseError as exc:
        # 凭据里存的 provider 名字现在不认识了 —— 只可能是改过名或者降级部署。
        raise NotConfiguredError(exc.message) from exc


def expires_soon(credential: PlatformCredential, *, now: datetime) -> bool:
    """token 快到期了没有。

    TikTok 的长期 token 没有 `expires_at`，所以这里对它恒为 False。留着是给
    Meta 的（60 天大限），真接的时候刷新逻辑挂在这个判据上。
    """
    return credential.expires_at is not None and credential.expires_at <= now


def _require_tiktok(settings: Settings) -> None:
    if not settings.tiktok_is_configured:
        raise NotConfiguredError(
            "未配置 TIKTOK_APP_ID / TIKTOK_APP_SECRET，无法发起或完成 TikTok 授权"
        )
    if not settings.oauth_redirect_base_url:
        raise NotConfiguredError(
            "未配置 OAUTH_REDIRECT_BASE_URL，拼不出回调地址（必须与开发者后台登记的一致）"
        )
