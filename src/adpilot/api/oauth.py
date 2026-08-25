"""OAuth 回调。**这是本项目第五条免认证的路由**，理由和防护都在下面。

## 为什么它不能要认证

平台把人**跳转**回这里（浏览器的 302），而浏览器跳转带不了 `Authorization` 头。
所以它只能进 `tests/test_auth_guard.py` 那份显式豁免清单 —— 那份清单存在的意义
就是让「又多了一个不要认证的入口」变成一次 review 时看得见的改动。

## 🔴 那它靠什么防住别人乱打

`state`：发起授权时用 `AUTH_SECRET` 签一个短期 token，回调时验签 + 验过期 +
验用途（`services/credential.py` 的 `verify_state`）。没有它，公网上任何人都能
诱导一次「把陌生的广告账户绑进这个系统」的写操作。

验签复用 `auth/token.py`，不另起一套 —— 那个模块里「`compare_digest` 而不是
`==`」「先验签再解析」有测试盯着源码形状，重写一份等于把那些保证扔掉。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from adpilot.api.deps import SessionDep, SettingsDep
from adpilot.api.errors import responses
from adpilot.schemas.fetch import CredentialRead
from adpilot.services import credential as credential_service

router = APIRouter(tags=["credentials"])


@router.get(
    "/oauth/tiktok/callback",
    response_model=CredentialRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="completeTikTokAuthorization",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
async def complete_tiktok_authorization(
    session: SessionDep,
    settings: SettingsDep,
    auth_code: Annotated[str, Query(description="平台回调带回来的一次性授权码")],
    state: Annotated[str, Query(description="发起授权时我们自己签的那个 state")],
) -> CredentialRead:
    """平台跳回来的落点：验 `state`、换 token、把凭据（密文）存下来。

    响应是刚建好的那条凭据，**里面没有 token**。看到它就说明授权成功了 ——
    回后台的凭据页刷新，把广告账户挂上去，自动拉取才真正开始。

    `state` 不合法或过期返回 404（对一个纯跳转地址，「这里没有东西」比「你的
    凭证不对」少泄露一点信息）；换 token 失败返回 502。

    ⚠️ **`auth_code` 是一次性的**，失败了不能刷新页面重试 —— 平台会告诉你这个
    码已经用过。重新从后台点一次「发起授权」。
    """
    label = credential_service.verify_state(settings, state)
    credential = await credential_service.create_from_auth_code(
        session,
        settings,
        auth_code=auth_code,
        label=label,
    )
    return CredentialRead.model_validate(credential)
