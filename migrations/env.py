"""Alembic 的运行环境。

两件事在这里发生，都直接对着仓库的硬规矩：

1. **DSN 从 `Settings` 取，不写进 `alembic.ini`。** ini 要进 git，而 DSN 带密码。
2. **`target_metadata` 认的是 `adpilot.models` 那份注册表。** 一个没被 import
   过的模型不会出现在 `Base.metadata` 里，autogenerate 于是生成一份空迁移，
   而且不报任何错 —— 所以这里只 import 那一个包，把「会不会被 import 到」
   收敛成 `models/__init__.py` 一处。

autogenerate 能检测什么、检测不到什么（改名、部分索引、PG 原生 ENUM 的值变更），
见 [Schema 方案第四节](../docs/design/2026-08-19-schema-migration.md)。
**生成完必须人看一遍**，这一步没有自动解。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import adpilot.models  # noqa: F401  —— import 本身就是目的：让模型注册进 metadata
from adpilot.config import get_settings
from adpilot.db.postgres import Base
from adpilot.models.types import StrEnumType

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ini 里那一行是空的，真正的 DSN 在这里注入。
config.set_main_option("sqlalchemy.url", get_settings().postgres_dsn)

target_metadata = Base.metadata

# compare_server_default 默认是关的 —— 关着的话，改了某列的 server_default
# 不会生成任何迁移，数据库和模型从此各说各话。代价是偶尔会因为「PG 把默认值
# 回显成另一种等价写法」多出一条无意义的 diff，那种时候手工删掉那一行即可，
# 比默认值漂移查不出来便宜。
COMPARE_OPTIONS = {
    "compare_type": True,
    "compare_server_default": True,
}


def render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """把自定义列类型渲染成 SQLAlchemy 自带的类型。返回 False 表示走默认渲染。

    🔴 **迁移文件必须自包含。** 默认渲染会往迁移里写
    `adpilot.models.types.StrEnumType(length=16)` —— 而且不会替你加那句 import，
    所以生成出来的文件当场就是坏的（`NameError`）。

    补上 import 只解决了症状。真正的问题是**迁移不该依赖应用代码**：迁移一旦
    提交就永不修改（改了 hash 对不上，别人的库也早跑过老版本了），而应用代码
    会被重构、改名、删掉 —— 那一天所有引用它的历史迁移一起崩。

    `StrEnumType` 在库里本来就是 varchar，渲染成 `sa.String` 的 DDL 完全等价，
    还顺带让迁移文件更好读：读的人不必去翻那个类才知道建出来的是什么列。
    """
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.impl.length})"
    return False


def run_migrations_offline() -> None:
    """只把 SQL 打印出来，不连数据库。

    `alembic upgrade head --sql` 走这条路，用途是把 DDL 交给 DBA 或审计流程。
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        **COMPARE_OPTIONS,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        **COMPARE_OPTIONS,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """连上数据库执行迁移。

    NullPool：迁移是一次性的短命进程，留着连接池没有意义，反而会让进程在
    命令跑完之后多挂几秒。
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
