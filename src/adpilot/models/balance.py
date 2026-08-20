"""账户余额快照。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.daily_metric import MONEY
from adpilot.models.mixins import TimestampMixin


class Balance(Base, TimestampMixin):
    """某个账户在某一时刻的可用余额。

    **是余额告警的依据，不是消耗。** 预充模式下余额归零广告直接停（不是降速），
    所以「还能撑几天」这条规则要的就是这张表最新的那一行。

    ### 为什么是 `captured_at` 而不是 `stat_date`

    余额是**时点量**，不是自然日量。同一天里充值前和充值后是两个完全不同的数，
    而 `stat_date` 那套「账户时区下的自然日」口径在这里表达不了「哪一刻」。所以
    这一列是 `timestamptz`，与 `daily_metrics.stat_date` 刻意不同 —— 两者回答的
    不是同一种问题。

    ### 只增不改

    同一个账户录第二条就是第二行，不覆盖。理由和 Mongo 里的 `raw_reports` 一样：
    「上周五余额还剩多少」是个会被回头追问的问题，而覆盖掉就再也答不上来。
    这里没有 append-only 的机器强制（它在 PG，不在那条硬规矩的覆盖范围内），
    但服务层只提供录入和查询，没有更新路径。
    """

    __tablename__ = "balances"

    __table_args__ = (
        # 同一个账户、同一时刻只能有一条。挡的是「点了两次提交」这种误操作 ——
        # 两条一模一样的快照不会让任何计算出错，但会让人在核对时怀疑自己看错了。
        UniqueConstraint("account_id", "captured_at"),
        # 规则引擎每次要的都是「这个账户最新的那条」，走的正是这个前缀。
        Index(None, "account_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    account_id: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"))

    #: 可用余额。金额一律 Decimal / numeric，理由见 conventions.md。
    available: Mapped[Decimal] = mapped_column(MONEY)

    # 币种在这行上再存一份，而不是每次 JOIN 回 ad_accounts 取 —— 和
    # `daily_metrics.currency` 同一个理由：账户币种可以被改，改了之后历史行仍要
    # 按当时的币种解释，否则过去的余额会被悄悄换成另一种货币。
    currency: Mapped[str] = mapped_column(String(3))

    #: 这个余额是**什么时候**的。不是录入时间 —— 人可能今天补录昨天看到的数。
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # 谁录的、从哪看来的、是不是刚充完值。日报里要说清「余额是什么时候的口径」，
    # 而这行字是唯一能说清「为什么当时是这个数」的地方。可空：大多数时候没什么
    # 可写的，强制填只会逼出一堆「无」。
    note: Mapped[str | None] = mapped_column(String(256))
