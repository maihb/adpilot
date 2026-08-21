"""广告账户。"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, SmallInteger, String, UniqueConstraint, text, true
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


#: 日切之后等几小时再自动生成日报的**默认值**。
#:
#: 定在这里是因为表结构是真相源，`schemas/` 和 `services/` 都引用它 —— 三处各写
#: 一个 2，改的时候必然漏掉一处，而漏掉的那处不会报错。
#:
#: ⚠️ **改它只影响以后新建的账户。** 列的 `server_default` 由迁移固化在数据库里，
#: 而迁移是历史、不该回头改 —— 已有的行要跟着变得写一条新的数据订正迁移。
DEFAULT_REPORT_DELAY_HOURS = 2


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

    #: 要不要每天自动出一份日报（生成成 draft，**绝不自动发布**）。
    #:
    #: 🔴 **它是省钱的闸门，不是审美偏好。** 并不是每个账户都要日报（内部测试
    #: 账户、只看看板的客户），而没有这个开关，那些账户每天白烧一次 LLM 调用 ——
    #: 自托管意味着花的是使用者自己的钱。
    #:
    #: **和 `is_active` 是两件事**：停投的账户仍然要日报（复盘那几天怎么停的），
    #: 而不看日报的账户可能一直在投。
    auto_report: Mapped[bool] = mapped_column(Boolean(), server_default=true())

    #: 账户时区下的日切之后，至少等这么多小时才自动生成那天的日报。
    #:
    #: ⚠️ **它防的不是「等数据稳定」** —— 平台数据在若干天内都还会变（glossary
    #: 的「回填与重述」），等不来。数字后来确实变了的处理方式是既有的那条：发新
    #: 的一份并注明重述，不动老的那份。
    #:
    #: 它防的是三件具体的事：平台自己日切的延迟、夏令时那天不是 24 小时、以及
    #: 接了 Ads API 之后「日切就拉」的冲动。在 CSV 世界里它几乎不起作用（「那天
    #: 有没有数据」本身就是更强的门），留着是为了接 API 那天不用改结构。
    #:
    #: 取值范围由 `schemas/ad_account.py` 卡（0–72），不在这里加 CHECK 约束 ——
    #: 那是 autogenerate 的盲区（见 docs/design/2026-08-19-schema-migration.md），
    #: 而这个值写错的后果只是日报早几小时或晚几小时，不值得为它引一条每次都要
    #: 手写迁移的约束。
    report_delay_hours: Mapped[int] = mapped_column(
        SmallInteger(), server_default=text(str(DEFAULT_REPORT_DELAY_HOURS))
    )

    # lazy="raise"：忘了 eager load 就当场报错，而不是在 async 下触发一次隐式
    # 懒加载 —— 那会抛 MissingGreenlet，报错信息与真正的原因（少写了一个
    # selectinload）之间毫无线索可循。
    client: Mapped[Client] = relationship(lazy="raise")
