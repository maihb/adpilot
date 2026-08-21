"""商品与库存快照：断货预警的依据。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.mixins import TimestampMixin

#: 件数的精度。**不是 `MONEY`** —— 语义不同，虽然此刻两者的定义碰巧一样。
#:
#: 为什么件数也用 `numeric` 而不是整数：按重量或长度卖的品（布料、零食散装）
#: 库存本来就带小数，而一个 `int` 列会把 12.5 米静默存成 12 或 13。小数位给到
#: 4 位是为了对齐 `MONEY`，让两张表的数字读起来一致。
QUANTITY = Numeric(20, 4)


class Product(Base, TimestampMixin):
    """客户店铺里的一个 SKU。

    ### 🔴 为什么挂在**客户**上，不挂广告账户

    一个客户可以同时投 Meta 和 TikTok 两个账户，推的是**同一批货**。挂在账户上
    就有两份库存记录，然后它们会打架：补货时要记得改两处，漏一处就是一条假告警，
    而那条告警长得跟真的一模一样。

    库存是**店铺的属性，不是投放账户的属性**。所以唯一键是 `(client_id, sku)`，
    而库存告警也因此是客户级的 —— `alerts.account_id` 可空正是为了这个
    （[库存断货设计][d]第三节）。

    [d]: ../../../docs/design/2026-08-21-stock-alerts.md

    ### 下架的款置 `is_active=False`，不删行

    同 `clients` / `ad_accounts`：历史快照挂在商品下面，删了它们就成了孤儿。
    巡检不看停用的商品。
    """

    __tablename__ = "products"

    __table_args__ = (
        # 同一个客户下 SKU 唯一 —— 导入是按 SKU 认商品的（文件里没有我们的 ID）。
        # 不同客户之间不互相约束：两家店各有一个 "A-001" 是完全正常的。
        UniqueConstraint("client_id", "sku"),
        # 巡检的查询形态：先按客户取，再筛在售的。
        Index(None, "client_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))

    #: 店铺侧的商品编码。**导入时的匹配键**，所以大小写和空格在服务层就归一化掉了。
    sku: Mapped[str] = mapped_column(String(128))

    #: 商品名，给人看的。导入时跟着文件更新（改名是常事），空着就退回显示 SKU。
    name: Mapped[str | None] = mapped_column(String(256))

    is_active: Mapped[bool] = mapped_column(Boolean(), server_default=true())


class StockSnapshot(Base, TimestampMixin):
    """某个商品在某一刻的库存。

    ### 和 `balances` 是同一种东西

    时点量、只增不改、`captured_at` 是「这个数属于哪一刻」而不是录入时间 ——
    三条都和余额快照一样，理由也一样（`models/balance.py` 的类 docstring）。

    ### 只有一处不同：同一时刻重复写是**覆盖**，不是冲突

    余额是人手敲的，同一个 (账户, 时刻) 出现两次基本是「点了两次提交」，所以那边
    回 409。库存是**文件上传**，重传同一份是常态（网络断了、发现少了一列重来），
    把它判成冲突等于逼人先去删数据。同一时刻的同一个 SKU 只能有一个库存数，后写
    的覆盖先写的即可 —— 这让整份导入是幂等的。
    """

    __tablename__ = "stock_snapshots"

    __table_args__ = (
        # 覆盖式导入靠它：`ON CONFLICT (product_id, captured_at) DO UPDATE`。
        UniqueConstraint("product_id", "captured_at"),
        # 规则要的是「这个商品最近的 N 条」，走的正是这个前缀。
        Index(None, "product_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))

    #: 可售库存件数。0 是真卖光了，不许为负。
    qty: Mapped[Decimal] = mapped_column(QUANTITY)

    #: 店铺后台导出的「近期日均销量」。
    #:
    #: **可空是这一列的重点**：不是所有店铺后台都导得出它。为空时日均由
    #: `rules/stock.py` 从快照序列自己推（相邻两次之间库存掉了多少），推不出来
    #: 就是「无定义、不告警」—— 与「分母为 0 返回 null」同一条规矩。
    daily_sales: Mapped[Decimal | None] = mapped_column(QUANTITY)

    #: 这份库存是**什么时候**的。不是上传时间 —— 人可能今天补传昨天导的表。
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    #: 这一批是从哪导的、导的时候是什么口径。可空，同 `balances.note`。
    note: Mapped[str | None] = mapped_column(String(256))
