"""客户。"""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.mixins import TimestampMixin


class Client(Base, TimestampMixin):
    """投放服务的对象，一个商家。

    **不是租户。** 本项目单实例单使用者，多客户是业务概念，不做数据隔离
    （[设计文档第七节](../../../docs/design/2026-08-19-mvp-design.md)）。所以
    这里没有、也不该有 `tenant_id`。

    停止合作的客户置 `is_active=False` 而不是删行：历史日报和结算记录都挂在
    客户下面，删了它们就成了孤儿。
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)

    # 唯一是为了让导入时能按名字幂等找回客户 —— 文件导入那条链路上，一份 CSV
    # 里通常只有客户名，没有 ID。
    name: Mapped[str] = mapped_column(String(128), unique=True)

    note: Mapped[str | None] = mapped_column(Text())
    is_active: Mapped[bool] = mapped_column(Boolean(), server_default=true())
