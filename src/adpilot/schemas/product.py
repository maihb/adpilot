"""商品、库存快照与断货预警的出入参。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ProductItem(BaseModel):
    """一个商品。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int

    #: 店铺侧的商品编码，导入时的匹配键。
    sku: str

    #: 商品名。可空 —— 有些导出只有编码和库存两列，页面那时退回显示 SKU。
    name: str | None

    #: 下架的款置 false，巡检不看它们。
    is_active: bool


class ProductListResponse(BaseModel):
    items: list[ProductItem]
    total: int


class StockRunwayResponse(BaseModel):
    """一个商品的库存还能撑多久。"""

    product_id: int
    client_id: int
    client_name: str
    sku: str
    name: str | None

    #: 最新一条快照上的可售库存。
    stock_qty: Decimal

    #: 日均销量。**`null` 表示算不出来**（只有一条快照，或者中间只发生过补货），
    #: 与「0」不是一回事 —— 后者是「近期一件没卖」。
    avg_daily_sales: Decimal | None

    #: 🔴 **`null` 表示无定义**，既不是 0 也不是「永远够用」。此时 `is_alerting`
    #: 一定是 false：没有动销就不会断货。
    days_left: Decimal | None

    #: 低于阈值即为真。阈值一并回出去，前端不必再抄一份。
    is_alerting: bool
    threshold_days: Decimal

    #: 日均销量是从哪来的：`file`（店铺导出自带那一列）/ `inferred`（由库存变化
    #: 推算）/ `none`（两条路都没走通）。
    #:
    #: **出参里带着它不是为了好看**：推算出来的日均建立在「中间没补过货」这个假设
    #: 上，而人看到「还能撑 2 天」时第一个该问的就是这个数字可信不可信。
    sales_source: str

    #: 用的是哪条快照（它说自己是什么时刻的）。
    captured_at: datetime

    #: 这个商品一共有几条快照（最多数到 `LOOKBACK_POINTS`）。为 1 时日均一定推
    #: 不出来，页面能据此说「再导一次就能算了」，而不是笼统的「算不出来」。
    snapshot_count: int


class StockRunwayListResponse(BaseModel):
    items: list[StockRunwayResponse]
    total: int


class StockImportResponse(BaseModel):
    """一次库存导入的结果。"""

    model_config = ConfigDict(from_attributes=True)

    client_id: int

    #: 这一批库存属于哪一刻。没传就是收到请求的时刻。
    captured_at: datetime

    products_created: int
    products_updated: int

    #: 落进去的快照条数 = 文件里有效的商品行数。
    snapshots: int

    #: 商品编码为空被跳过的行数（导出末尾的合计行、分类之间的空行）。**不是错误**，
    #: 但数字要报出来 —— 静默丢掉会让「导进去的条数对不上」变成一桩无头案。
    skipped_rows: int

    #: 文件里带了日均销量列的条目数。**为 0 时日均要靠快照序列推**，而那需要至少
    #: 两次导入才有结果 —— 页面该据此提示，否则第一次导完看到一屏「算不出来」
    #: 会让人以为导失败了。
    with_sales_column: int
