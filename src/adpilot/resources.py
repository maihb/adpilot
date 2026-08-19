"""外部资源容器：进程级创建一次，供所有请求共享。

连接池是进程级的，不是每请求一份：启动时打开，关闭时释放。handler 通过 FastAPI
依赖（`adpilot.api.deps`）拿到它们，而不是 import 全局变量 —— 这样测试才能塞
替身进来，不用去 monkeypatch 模块状态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from adpilot.config import Settings
from adpilot.db import mongo, postgres
from adpilot.db import redis as redis_db
from adpilot.db.mongo import MongoClient, MongoDatabase


@dataclass(slots=True)
class Resources:
    """本进程会打交道的所有外部系统的句柄。"""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    mongo_client: MongoClient
    mongo_db: MongoDatabase
    redis: Redis


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    """打开全部连接池，退出时逐个关闭。

    构造客户端并不会真的连上去 —— 三个驱动都是懒连接。这是故意的：某个依赖短暂
    挂掉时进程仍然要能起来，「连不上」该由就绪探针报出来，而不是由构造函数。
    """
    engine = postgres.create_engine(settings)
    mongo_client = mongo.create_client(settings)
    redis_client = redis_db.create_client(settings)

    try:
        yield Resources(
            settings=settings,
            engine=engine,
            session_factory=postgres.create_session_factory(engine),
            mongo_client=mongo_client,
            mongo_db=mongo.get_database(mongo_client, settings),
            redis=redis_client,
        )
    finally:
        await redis_client.aclose()
        mongo_client.close()
        await engine.dispose()
