"""FastAPI 依赖。

handler 通过这些函数索取它需要的东西，而不是 import 模块级全局变量，
这样测试才能单独覆盖其中任意一个。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings
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


ResourcesDep = Annotated[Resources, Depends(get_resources)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
