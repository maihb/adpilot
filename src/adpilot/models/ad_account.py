"""广告账户。"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adpilot.db.postgres import Base
from adpilot.models.client import Client
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType


class Platform(StrEnum):
    """投放平台。

    值会随接入的平台增加。存的是 varchar 不是 PG 原生 ENUM，所以加一个成员
    **不需要迁移** —— 理由见 `models/types.py` 的 `StrEnumType`。
    """

    META = "meta"
    TIKTOK = "tiktok"


class AdAccount(Base, TimestampMixin):
    """平台侧的投放账户，归属某个客户。

    **不是登录账号。** 一个客户可以有多个账户，跨平台也跨币种。

    这张表上有三个决定了整套指标怎么解释的字段，缺一个后面就说不清数字：

    * `timezone` —— `stat_date` 的口径依据。广告账户时区、店铺时区、看报表的人
      所在时区可以是三个不同的值，日切点因此能差好几个小时
      （[glossary](../../../docs/business/glossary.md) 的「时间口径」一节）。
    * `currency` —— `spend` 是账户币种，不是人民币。跨账户汇总前必须先说清楚
      要不要换算。
    * `external_id` —— 平台侧的账户 ID，是回到平台核对时唯一的锚点。
    """

    __tablename__ = "ad_accounts"

    __table_args__ = (
        # 同一个平台上，一个账户 ID 只能出现一次。导入是按 (平台, 账户 ID) 找回
        # 账户的，没有这条约束，重复建一个同名账户会让同一天的数据分裂到两行。
        UniqueConstraint("platform", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    client_id: Mapped[int] = mapped_column(
        # RESTRICT：客户下面还挂着账户就不许删。停止合作走 Client.is_active，
        # 删除从来不是这个系统里的正常操作。
        ForeignKey("clients.id", ondelete="RESTRICT"),
        index=True,
    )

    platform: Mapped[Platform] = mapped_column(StrEnumType(Platform, 16))
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))

    # ISO 4217 三字母代码（USD / CNY / …）。
    currency: Mapped[str] = mapped_column(String(3))

    # IANA 时区名（America/Anchorage 这种），不是 UTC 偏移量 —— 偏移量在夏令时
    # 切换那天是错的，而广告数据恰恰按自然日切。
    timezone: Mapped[str] = mapped_column(String(64))

    is_active: Mapped[bool] = mapped_column(Boolean(), server_default=true())

    # lazy="raise"：忘了 eager load 就当场报错，而不是在 async 下触发一次隐式
    # 懒加载 —— 那会抛 MissingGreenlet，报错信息与真正的原因（少写了一个
    # selectinload）之间毫无线索可循。
    client: Mapped[Client] = relationship(lazy="raise")
