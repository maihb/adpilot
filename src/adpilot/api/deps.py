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


ResourcesDep = Annotated[Resources, Depends(get_resources)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
MongoDep = Annotated[MongoDatabase, Depends(get_mongo)]
