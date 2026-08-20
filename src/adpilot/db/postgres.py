"""PostgreSQL 引擎与会话生命周期。

PostgreSQL 存的是需要事务和 JOIN 的事实：客户、广告账户、归一化后的日指标、
日报与结算。平台返回的原始 payload **不放这里**，理由见 `adpilot.db.mongo`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from adpilot.config import Settings

# 约束与索引的命名模板。不配这个的话，名字由 PostgreSQL 自动生成
# （`ad_accounts_platform_external_id_key` 这种），于是迁移里的 drop/alter 得
# 引用一个不由我们控制的名字 —— 而 Alembic 只有拿得到稳定的名字，生成的 diff
# 才稳定、才可读。这件事必须在**第一份迁移之前**定死：表建出来之后再改约定，
# 等于给每一条已有约束手写一次改名。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全项目 ORM 模型的声明式基类。

    模型定义都在 `adpilot.models`，那里是表结构的真相源；这里只提供基类、
    上面那套命名约定，以及下面这个 async 下必须打开的开关。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # eager_defaults=True 让 **UPDATE** 也带上 RETURNING，把服务端生成的值当场
    # 取回来。默认的 "auto" 只对 INSERT 这么做，于是 `TimestampMixin` 那个
    # `onupdate=func.now()` 在一次 UPDATE 之后处于 expired 状态 —— 谁碰它谁就
    # 触发一次隐式的属性加载。
    #
    # 在同步 SQLAlchemy 里这只是多一条 SELECT，在 async 下是**崩**：出参序列化
    # （Pydantic 的 model_validate）是同步调用，触发懒加载就抛 MissingGreenlet，
    # 而那个报错跟真正的原因（某个列被 expire 了）之间毫无线索可循。

    # 下面那行压掉 RUF012 的理由（注意别让本行以 noqa 开头，ruff 会把它当成一条
    # 真的指令）：SQLAlchemy 在 DeclarativeBase 上把 `__mapper_args__` 声明成了
    # 实例变量，按 RUF012 的建议标 ClassVar，mypy 就报「不能用类变量覆盖实例变量」
    # —— 两个门禁在这一行上互斥。这里的字典是类级配置，不存在 RUF012 真正担心的
    # 「实例间共享可变状态」。
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012


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

    这是**生成器**形态，给 FastAPI 的 `Depends` 用（它会在响应发出后把生成器驱动
    到底，`commit()` 才跑得到）。自己写循环的调用方用下面那个 `transaction`。
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# 🔴 同一个 scope 的 `async with` 形态，给没有依赖注入机制的调用方（Celery 任务）用。
#
# **不要自己写 `async for session in session_scope(...): return ...`。** 在
# `async for` 里 return 会让生成器在 `yield` 那一行收到 `GeneratorExit`，而
# `GeneratorExit` 继承的是 `BaseException` —— 上面那个 `except Exception` 接不住它，
# `commit()` 也永远跑不到。结果是事务被静默丢弃：接口测试全绿（用的是 Depends 那条
# 路径），任务写的数据却一行都没落地。
transaction = asynccontextmanager(session_scope)
