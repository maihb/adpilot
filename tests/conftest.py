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
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    """
    return Settings(
        environment=Environment.TEST,
        postgres_host="127.0.0.1",
        postgres_port=1,
        postgres_password=SecretStr("unused"),
        mongo_uri=SecretStr("mongodb://127.0.0.1:1"),
        redis_url=SecretStr("redis://127.0.0.1:1"),
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
