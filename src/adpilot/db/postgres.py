"""PostgreSQL 引擎与会话生命周期。

PostgreSQL 存的是需要事务和 JOIN 的事实：客户、广告账户、归一化后的日指标、
日报与结算。平台返回的原始 payload **不放这里**，理由见 `adpilot.db.mongo`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from adpilot.config import Settings


class Base(DeclarativeBase):
    """全项目 ORM 模型的声明式基类。"""


def create_engine(settings: Settings) -> AsyncEngine:
    """构造异步引擎。

    `pool_pre_ping` 每次取连接多花一个 round trip，换来的是对「空闲期被掐断的
    连接」免疫 —— 数据库挂在云负载均衡后面时，空闲超时踢连接是常态。
    """
    return create_async_engine(
        settings.postgres_dsn,
        echo=settings.debug,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """构造绑定到 `engine` 的会话工厂。

    `expire_on_commit=False` 让 ORM 对象在 commit 之后仍然可用，handler 才能
    直接序列化响应，而不会突然触发一次意料之外的懒加载。
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """产出一个会话：成功则提交，异常则回滚。

    业务代码一律不手写事务，全走这个 scope —— 「某个分支忘了回滚」这种事
    从机制上就不会发生。
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
