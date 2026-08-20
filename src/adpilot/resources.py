"""外部资源容器：进程级创建一次，供所有请求共享。

连接池是进程级的，不是每请求一份：启动时打开，关闭时释放。handler 通过 FastAPI
依赖（`adpilot.api.deps`）拿到它们，而不是 import 全局变量 —— 这样测试才能塞
替身进来，不用去 monkeypatch 模块状态。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from adpilot.config import Settings
from adpilot.db import broker, mongo, postgres
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

    #: 任务队列。接口进程拿它**投递**任务（`send_task`），worker 拿它消费。
    celery: Celery


@asynccontextmanager
async def open_resources(
    settings: Settings,
    *,
    celery: Celery | None = None,
) -> AsyncGenerator[Resources, None]:
    """打开全部连接池，退出时逐个关闭。

    构造客户端并不会真的连上去 —— 四个驱动都是懒连接。这是故意的：某个依赖短暂
    挂掉时进程仍然要能起来，「连不上」该由就绪探针报出来，而不是由构造函数。

    `celery` 给了就用给的那个，不给就现建一个。**worker 必须传自己那个** ——
    它已经有一个绑好了任务的 app（`tasks/app.py`），这里再建一个的话，进程里
    就有两个 Celery 实例各持一条 broker 连接，而任务只注册在其中一个上。
    """
    engine = postgres.create_engine(settings)
    mongo_client = mongo.create_client(settings)
    redis_client = redis_db.create_client(settings)
    celery_app = celery if celery is not None else broker.create_celery_app(settings)

    try:
        yield Resources(
            settings=settings,
            engine=engine,
            session_factory=postgres.create_session_factory(engine),
            mongo_client=mongo_client,
            mongo_db=mongo.get_database(mongo_client, settings),
            redis=redis_client,
            celery=celery_app,
        )
    finally:
        celery_app.close()
        await redis_client.aclose()
        mongo_client.close()
        await engine.dispose()
