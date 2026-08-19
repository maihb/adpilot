"""自定义列类型。

目前只有一个：把 Python 的 `StrEnum` 存成普通 varchar。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from sqlalchemy import Dialect, String
from sqlalchemy.types import TypeDecorator

E = TypeVar("E", bound=StrEnum)


class StrEnumType(TypeDecorator[E]):
    """存成 varchar、读回来是枚举成员的列类型。

    **为什么不用 SQLAlchemy 自带的 `Enum(...)`**：它默认建 PostgreSQL 原生
    ENUM，而原生 ENUM 加一个值要 `ALTER TYPE ... ADD VALUE`，那条语句在事务里
    有限制，Alembic 的 autogenerate 也基本管不了它。广告平台的枚举恰恰是会不断
    加值的（新平台、新层级、新状态）—— 这正是
    [Schema 方案第五节约定 2](../../docs/design/2026-08-19-schema-migration.md)
    要绕开的盲区。`native_enum=False` 也不解决问题：它退化成 varchar + CHECK
    约束，而 CHECK 同样是 autogenerate 的盲区，加一个值仍要手写迁移。

    所以列上就是裸 varchar，**取值的合法性由 Python 侧保证**：写进去的必须是
    枚举成员，读出来一律转回枚举。数据库里真躺着一个没见过的值时，转换会在
    这里当场抛 `ValueError`，而不是让一个来路不明的字符串一路漂进业务逻辑 ——
    后者的症状是某个 `if level == ...` 分支永远不成立，很难查。
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_type: type[E], length: int = 32) -> None:
        self._enum_type = enum_type
        super().__init__(length=length)

    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        # 先过一遍枚举构造：调用方给的是裸字符串时，这一步就把它拦下来。
        return self._enum_type(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self._enum_type(value)
