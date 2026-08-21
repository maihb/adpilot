"""投放操作记录：日报里「本期做了什么」的唯一数据来源。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.daily_metric import MetricLevel
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType


class ActionKind(StrEnum):
    """调整的类型。存的是 varchar，加一个成员**不需要迁移**（见 `models/types.py`）。

    分这几类是为了让「本期做了什么」**可数**（设计文档第十节第 4 条）—— 日报要能
    写出「本周改了 3 次预算、换了 2 组素材」，而那句话只有在类型是枚举、不是自由
    文本时才写得出来。分不进去的写 `OTHER` 并在 `summary` 里说清楚。
    """

    BUDGET = "budget"
    BID = "bid"
    CREATIVE = "creative"
    TARGETING = "targeting"

    #: 起停：开广告、关广告、暂停系列。
    STATUS = "status"

    OTHER = "other"


class ActionSource(StrEnum):
    """这条记录是人填的还是从平台抓来的。

    `PLATFORM` 现在没有任何写入路径 —— 它是给「接了 Ads API 之后自动抓变更日志」
    留的位置。先把这一列立起来，是因为那天两种来源必须能分辨：自动抓来的记录
    **没有 `reason`**（平台不记录人为什么这么调），而日报里真正有价值的恰恰是
    那一段。混成一列的话，届时只能靠 `reason` 空不空去猜来源。
    """

    MANUAL = "manual"
    PLATFORM = "platform"


class Action(Base, TimestampMixin):
    """一次投放调整：改了什么、为什么改、什么时候改的。

    ### 🔴 `reason` 必填，这是这张表存在的理由

    [设计文档第十节第 4 条](../../../docs/design/2026-08-19-mvp-design.md)把话说死
    了：日报里「本期做了什么」是交付价值所在，而**自动抓取平台变更日志拿不到「为
    什么这么调」**。「把 A 系列日预算从 500 提到 800」平台自己就有记录；「因为周末
    CPM 普涨、先扛量到周一再看」只存在于操作的人脑子里，不当场记下来，三天后就没
    了。所以手动登记不是自动化没做完之前的妥协，它记的是另一样东西。

    落到列上：`summary` 可以照抄平台，`reason` 不行 —— 它是 `Text` 且 `NOT NULL`。

    ### 为什么是 `performed_at` 而不是 `stat_date`

    操作是**时点**发生的（和 `balances.captured_at` 同源），而日报按「账户时区下
    的自然日」组织。两者的换算**不冗余存一列**，理由是账户时区可以被改，改完之后
    冗余的那一列就和 `performed_at` 各说各话了。

    代价是查询侧要自己换算，而那件事**只在一个地方做**：`services/action.py` 的
    `window_bounds`。跨时区的日切算错不会报错，只会让某条记录在日报里出现在前一天
    ——所以它在业务文档里被标成「必须读源码」。

    ### 为什么没有 `detail` 这种 JSONB 列

    告警那边有一列 `detail`（触发时算出来的数字），这里刻意不给。手动登记的场景下
    人写的就是 `summary` 那一行人话，多一个自由字典只会诱使人往里塞结构，而没有
    任何东西消费它。等 `PLATFORM` 那条来源真的接进来、前后值由平台给出时，再加这
    一列，那时它有明确的生产者和消费者。
    """

    __tablename__ = "actions"

    __table_args__ = (
        # 日报的查询形态：某个账户、某个时刻区间。没有别的查法 —— 「所有账户今天
        # 做了什么」不是这个系统要回答的问题。
        Index(None, "account_id", "performed_at"),
    )

    # 手动登记时这张表一天只有几行，int4 绰绰有余；但 `PLATFORM` 那条来源接进来
    # 之后它会跟着平台变更日志按天增长。现在多这 4 个字节没有代价，真撞上 21 亿
    # 再改成 bigint 要重写整张表。
    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)

    account_id: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"))

    kind: Mapped[ActionKind] = mapped_column(StrEnumType(ActionKind, 16))
    source: Mapped[ActionSource] = mapped_column(StrEnumType(ActionSource, 16))

    # 动在哪一级、哪个对象上。层级复用 `daily_metrics` 那套四级枚举，日报才能把
    # 「改了这个系列的预算」和这个系列当天的花费对上 —— 两边各定义一套枚举的话，
    # 对不上的那天没有任何东西会报错。
    #
    # 账户级调整（比如整体调了日预算）填 `ACCOUNT`，此时 `object_id` 为空：账户
    # 自己的 external_id 填进来也不增加任何信息。
    level: Mapped[MetricLevel] = mapped_column(StrEnumType(MetricLevel, 16))
    object_id: Mapped[str | None] = mapped_column(String(64))

    # 平台侧的展示名，会被人改。留一份是为了日报里写得出人看得懂的名字，
    # **不作为标识** —— 认对象一律认 object_id（同 `daily_metrics.object_name`）。
    object_name: Mapped[str | None] = mapped_column(String(256))

    #: 做了什么，一行人话，直接能进日报（「A 系列日预算 500 → 800」）。
    summary: Mapped[str] = mapped_column(String(256))

    #: 🔴 为什么这么做。见类 docstring —— 这一列是这张表和平台变更日志的唯一区别。
    reason: Mapped[str] = mapped_column(Text())

    #: 操作**实际发生**的时刻，不是登记时刻（登记时刻是 `created_at`）。人常常
    #: 下班前补登记白天做的几件事，两者差几个小时是常态。
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # 谁做的。运营账号只有一个（不建 users 表，理由见 config.py），所以这里是
    # 自由文本而不是外键 —— 多人共用一个账号时，token 里的 sub 恒等于 `admin`，
    # 记下来等于没记。可空：一个人的团队填它没有意义。
    operator: Mapped[str | None] = mapped_column(String(64))
