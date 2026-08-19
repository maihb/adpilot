"""归一化后的日指标。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType

# 金额与「可能带小数的计数」统一的精度。20 位总长够放任何币种的年度花费，
# 4 位小数够放分币种的最小单位与按比例归因出来的小数转化数。
#
# ⚠️ PostgreSQL 的 numeric(20,4) 对超出精度的写入是**四舍五入**（round half
# away from zero），既不报错也不截断。原始值小数位更多时，落库的就是舍入后的数。
MONEY = Numeric(20, 4)


class MetricLevel(StrEnum):
    """归一化后的四级投放层级。

    平台叫法不统一（Meta 的 ad set、TikTok 的 ad group），**归一化时一律映射到
    `ADGROUP`** —— 映射表是唯一收口点，见
    [glossary](../../../docs/business/glossary.md)。
    """

    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    ADGROUP = "adgroup"
    AD = "ad"


class DailyMetric(Base, TimestampMixin):
    """某账户、某层级、某对象、某个自然日的归一化指标。

    **不是平台原始行** —— 原始行在 Mongo 的 `raw_reports` 里，永不修改，
    随时可以拿它重跑归一化。

    ### 派生指标为什么一列都不存

    CPM / CPC / CTR / CPA / ROAS 全部按 [glossary](../../../docs/business/glossary.md)
    的公式**现算**。存下来就等于把公式复制进了数据库：口径一改、或者平台回填了
    历史数据，存的那份和算的那份立刻对不上，而「同一个 CPA 出现两个值」会让整套
    输出的可信度归零。

    ### 时间口径

    `stat_date` 是**广告账户时区下的自然日**，时区记在 `ad_accounts.timezone`。
    跨账户汇总时不要把不同时区的同一个 `stat_date` 当成同一天直接相加 —— 能加，
    但要知道加的是什么，并在日报里注明。
    """

    __tablename__ = "daily_metrics"

    __table_args__ = (
        # 幂等重导的依据。平台数据在若干天内还会变（归因回传、无效流量剔除、
        # 汇率重算），所以同一个 (账户, 层级, 对象, 日期) 要按这条唯一键 upsert。
        # **没有它，同一天导两次就是双倍花费。**
        UniqueConstraint("account_id", "level", "object_id", "stat_date"),
        # 上面那条唯一索引的前导列是 (account_id, level)，按「某账户某个日期区间」
        # 查时用不上它。看板和日报走的恰恰是这个查询形态，所以单独建一条。
        # 名字给 None，交给 Base.metadata 的命名约定推 —— 手写一个名字就等于
        # 在约定之外又立了一份，两者早晚会不一致。
        Index(None, "account_id", "stat_date"),
    )

    # 这是唯一一张按天线性增长的表（账户数 × 四个层级 × 对象数 × 天数），主键用
    # bigint。int4 的 21 亿上限在自托管规模下大概率够用，但「大概率」不值得赌：
    # 真撞上了要改成 bigint 得重写整张表，而现在多这 4 个字节没有任何代价。
    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)

    account_id: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"))
    level: Mapped[MetricLevel] = mapped_column(StrEnumType(MetricLevel, 16))

    # 平台侧的对象 ID。level=ACCOUNT 时填账户自己的 external_id —— 让唯一键在
    # 四个层级上都是同一个形状，聚合查询不必为账户级单开一条分支。
    object_id: Mapped[str] = mapped_column(String(64))

    # 对象名是平台侧的展示名，会被人改。留一份是为了日报里能写出人看得懂的名字，
    # **不作为标识**：认对象一律认 object_id。
    object_name: Mapped[str | None] = mapped_column(String(256))

    stat_date: Mapped[date] = mapped_column(Date())

    # 币种在指标行上再存一份，而不是每次 JOIN 回 ad_accounts 去取：账户币种是
    # 可以被改的，改了之后历史行必须仍然按当时的币种解释，否则过去的花费会被
    # 悄悄换成另一种货币。
    currency: Mapped[str] = mapped_column(String(3))

    spend: Mapped[Decimal] = mapped_column(MONEY)
    impressions: Mapped[int] = mapped_column(BigInteger())
    clicks: Mapped[int] = mapped_column(BigInteger())

    # 转化数用 numeric 不用整数：平台按归因比例分配时会给出小数（半个转化算给
    # 这条广告的情形是真实存在的）。取哪个转化事件必须显式配置，见 glossary。
    conversions: Mapped[Decimal] = mapped_column(MONEY)
    revenue: Mapped[Decimal] = mapped_column(MONEY)

    # 🔴 reach 跨天不可加，frequency 由它算出来同理。所以 frequency 不存 ——
    # 周期汇总的 reach 必须向平台单独请求那个周期的值，不能拿日数据加出来。
    # 可空是因为平台不一定给这个字段。
    reach: Mapped[int | None] = mapped_column(BigInteger())
