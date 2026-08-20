"""FastAPI 依赖。

handler 通过这些函数索取它需要的东西，而不是 import 模块级全局变量，
这样测试才能单独覆盖其中任意一个。

认证的两个依赖（`OperatorDep` / `ClientScopeDep`）也在这里。它们各自绑一个
**独立的** security scheme，于是「这个接口要哪种身份」会出现在 openapi.json 的
`security` 字段里 —— `tests/test_auth_guard.py` 的两道门禁就是读那里判的，
不必去反射 FastAPI 的依赖树。挑一个「忘了就注册不出来」的东西去验，和
`test_business_docs.py` 拿 tag 当锚点是同一个套路。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from celery import Celery
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.auth.token import InvalidTokenError, Scope, TokenPayload, verify
from adpilot.config import Settings
from adpilot.db.mongo import MongoDatabase
from adpilot.db.postgres import session_scope
from adpilot.resources import Resources


def get_resources(request: Request) -> Resources:
    """返回进程级资源容器。"""
    resources: Resources = request.app.state.resources
    return resources


def get_settings(resources: Annotated[Resources, Depends(get_resources)]) -> Settings:
    return resources.settings


async def get_session(
    resources: Annotated[Resources, Depends(get_resources)],
) -> AsyncIterator[AsyncSession]:
    """产出一个事务性数据库会话，作用域为单次请求。"""
    async for session in session_scope(resources.session_factory):
        yield session


def get_mongo(resources: Annotated[Resources, Depends(get_resources)]) -> MongoDatabase:
    """原始快照所在的 Mongo 库。

    与 `get_session` 不同，这里**没有事务** —— Mongo 侧的写入是 append-only 的
    单条 insert，没有需要一起提交或一起回滚的东西。所以一次导入请求里，PG 那边
    回滚了、Mongo 这边的快照仍然留着：这是刻意的取舍，快照多一条不伤害任何人，
    而丢一条就再也拿不回「当时那个数」了。
    """
    return resources.mongo_db


def get_celery(resources: Annotated[Resources, Depends(get_resources)]) -> Celery:
    """任务队列的**生产者**句柄。

    接口进程只投递、不消费，也不 import 任何任务代码 —— 任务按名字发，名字常量在
    `db/broker.py`。消费那一侧是 `adpilot.tasks` 和另一个进程的事。
    """
    return resources.celery


ResourcesDep = Annotated[Resources, Depends(get_resources)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
MongoDep = Annotated[MongoDatabase, Depends(get_mongo)]
CeleryDep = Annotated[Celery, Depends(get_celery)]


# --- 认证 -------------------------------------------------------------------
#
# 两个 scheme 而不是一个：名字会原样出现在 openapi.json 的 `security` 里，
# 门禁测试据此分辨「这个接口要的是运营身份还是客户身份」。合成一个的话，
# 「客户端路由漏了作用域」就再也验不出来了。
#
# ⚠️ `auto_error=False` 不能省。`HTTPBearer` 默认在缺 Authorization 头时返回
# **403**，而这里该是 401 —— 403 的语义是「你是谁我知道，但不让你进」，
# 会让客户端不去走重新登录的流程。关掉之后由下面的代码统一抛
# `InvalidTokenError`，`api/errors.py` 翻成带 WWW-Authenticate 的 401。
_operator_scheme = HTTPBearer(
    scheme_name="OperatorBearer",
    description="运营 token，POST /api/auth/login 换取",
    auto_error=False,
)
_client_scheme = HTTPBearer(
    scheme_name="ClientBearer",
    description="客户端 token，POST /api/auth/redeem 用邀请码换取",
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """验过的 token：解出来的内容，以及原始那一串。

    续期接口要的是原始串（`renew` 收 token 不收 payload），别的接口要的是内容。
    合在一个依赖里是为了**只验一次签** —— FastAPI 在单次请求内缓存依赖结果，
    所以下面那两个薄依赖共用这一次校验。
    """

    payload: TokenPayload
    token: str


def _authenticate(
    settings: Settings,
    credentials: HTTPAuthorizationCredentials | None,
    scope: Scope,
) -> AuthContext:
    if credentials is None:
        # 没带头和带了个坏 token 是同一种失败，理由见 api/errors.py。
        raise InvalidTokenError("缺少 Authorization 头")
    payload = verify(settings.auth_secret, credentials.credentials, scope=scope)
    return AuthContext(payload=payload, token=credentials.credentials)


def get_operator_context(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_operator_scheme)],
) -> AuthContext:
    return _authenticate(settings, credentials, Scope.OPERATOR)


def get_client_context(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_client_scheme)],
) -> AuthContext:
    return _authenticate(settings, credentials, Scope.CLIENT)


def require_operator(
    context: Annotated[AuthContext, Depends(get_operator_context)],
) -> TokenPayload:
    """运营身份。挂在内部那几组路由上，一律经 `main.py` 统一挂，不逐个 handler 写。"""
    return context.payload


OperatorDep = Annotated[TokenPayload, Depends(require_operator)]
OperatorContextDep = Annotated[AuthContext, Depends(get_operator_context)]
ClientContextDep = Annotated[AuthContext, Depends(get_client_context)]
