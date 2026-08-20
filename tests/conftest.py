"""共享测试夹具。

本套件里有两类测试：

* **单元测试** —— 不依赖任何外部服务，验证接线和失败路径，必须在一台只装了
  Python 的机器上也能全绿。
* **集成测试**（`@pytest.mark.integration`）—— 需要 `docker-compose.yml` 里那套
  真实的 PostgreSQL / MongoDB / Redis。CI 用 service container 起它们；本地默认
  跳过，除非显式设 `RUN_INTEGRATION=1` —— 免得「没起数据库」看起来像「测试挂了」。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adpilot.api.deps import get_session
from adpilot.config import Environment, Settings
from adpilot.db.postgres import create_engine
from adpilot.main import create_app


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """没有显式开关就跳过集成测试。"""
    if os.getenv("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="需要后端服务，设 RUN_INTEGRATION=1 才跑")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def offline_settings() -> Settings:
    """指向「没人监听」的端口的配置。

    用来证明就绪探针在依赖不可达时能干净降级，而不是卡住或抛异常。

    `_env_file=None` 是必须的：不关掉的话，本机 .env 会把没显式传的字段补上，
    这个「哪儿都连不上」的场景就未必造得出来 —— 而失败只发生在某些人的机器上。
    """
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host="127.0.0.1",
        postgres_port=1,
        postgres_password=SecretStr("unused"),
        mongo_host="127.0.0.1",
        mongo_port=1,
        mongo_password=SecretStr("unused"),
        redis_host="127.0.0.1",
        redis_port=1,
    )


@pytest.fixture
def offline_app(offline_settings: Settings) -> FastAPI:
    return create_app(offline_settings)


@pytest.fixture
def offline_client(offline_app: FastAPI) -> Iterator[TestClient]:
    """所有后端服务都连不上的测试客户端。"""
    with TestClient(offline_app) as client:
        yield client


@pytest.fixture
def live_settings() -> Settings:
    """从环境变量读取的配置，供集成测试使用。"""
    return Settings(environment=Environment.TEST)


@pytest.fixture
def live_client(live_settings: Settings) -> Iterator[TestClient]:
    """接到真实后端服务上的测试客户端。"""
    with TestClient(create_app(live_settings)) as client:
        yield client


@pytest.fixture
async def live_session(live_settings: Settings) -> AsyncIterator[AsyncSession]:
    """连真实 PostgreSQL 的会话，**用例结束整体回滚**。

    表得先存在 —— 跑之前先 `make migrate`（CI 里那一步在集成测试之前）。

    隔离靠的是「把 session 挂进一个外层事务」：`join_transaction_mode` 设成
    `create_savepoint`，用例里照常 `commit()` 也只是释放一个 SAVEPOINT，最后
    外层一 rollback 就什么都没留下。不这么做的话，用例之间会靠残留数据互相
    影响，而那种失败只在特定执行顺序下出现，最难查。
    """
    engine = create_engine(live_settings)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def live_api(
    live_settings: Settings,
    live_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """打真实数据库、但用例结束整体回滚的 HTTP 客户端。

    **为什么不是 `TestClient`。** 它在自己的线程里跑一个独立的事件循环，而
    asyncpg 的连接绑定在创建它的那个循环上 —— 把 `live_session` 交给它，第一次
    查询就会炸在「attached to a different loop」上。`ASGITransport` 直接在当前
    循环里调用应用，两边才是同一个 loop。

    override 掉 `get_session` 顺带带来一个好处：应用不再需要
    `app.state.resources`，所以这里不跑 lifespan，这组测试也就不依赖 Mongo 和
    Redis 起没起 —— 它们验的是 HTTP 契约和 PostgreSQL 那条链路。

    代价要知道：真实请求走的是 `session_scope`（返回即 commit），而这里的会话
    由夹具管，**handler 里不会发生真正的提交**。提交这一步由 `session_scope`
    保证，`test_health.py` 的集成用例覆盖了那条路径。
    """
    app = create_app(live_settings)
    app.dependency_overrides[get_session] = lambda: live_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as api:
        yield api
