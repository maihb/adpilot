"""商品、库存快照与断货告警。

分工同 `services/balance.py`：**查库在这里，算账在 `rules/stock.py`。** 这一层
额外还多一件事 —— 把一份 CSV 变成「商品 upsert + 快照 upsert」，那是库存与余额
最大的形状差异（余额是一个数一次录，库存是几十上百个 SKU 一次导）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.ad_account import AdAccount
from adpilot.models.client import Client
from adpilot.models.product import Product, StockSnapshot
from adpilot.providers.base import ParseError
from adpilot.providers.stock_csv import StockCsvParser
from adpilot.rules import stock as stock_rules
from adpilot.services import client as client_service
from adpilot.services.exceptions import InvalidDataError, NotFoundError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StockImportSummary:
    """一次库存导入的结果。**不含条目本身** —— 那可能是几百行。"""

    client_id: int
    captured_at: datetime
    products_created: int
    products_updated: int
    snapshots: int
    skipped_rows: int

    #: 文件里带了日均销量列的条目数。0 表示整份文件都没有那一列，日均将由
    #: `rules/stock.py` 从快照序列推 —— 而那需要**至少两次**导入才有结果，
    #: 所以这个数字要报给导入的人看。
    with_sales_column: int


@dataclass(frozen=True, slots=True)
class StockAlert:
    """一个商品此刻的库存状况。`runway.is_alerting` 为真就是要告警的那些。"""

    product_id: int
    client_id: int
    client_name: str
    sku: str
    name: str | None
    runway: stock_rules.StockRunway
    captured_at: datetime

    #: 日均销量是从哪来的：`file` 表示导出文件自带那一列，`inferred` 表示由快照
    #: 序列推出来，`none` 表示两条路都没走通。
    #:
    #: 出参里带着它不是为了好看：推出来的日均**建立在「中间没补过货」这个假设**
    #: 上，而人看到「还能撑 2 天」时，第一个该问的就是这个数字可信不可信。
    sales_source: str

    #: 这个商品一共有几条快照。1 条时日均一定推不出来，页面要能说清「再导一次
    #: 就能算了」，而不是笼统的「算不出来」。
    snapshot_count: int


#: `sales_source` 的三个取值。写成常量是为了让服务层、出参和前端对同一组字面量。
SALES_FROM_FILE = "file"
SALES_INFERRED = "inferred"
SALES_UNKNOWN = "none"


async def import_stock(
    session: AsyncSession,
    *,
    client_id: int,
    content: bytes,
    captured_at: datetime,
    note: str | None = None,
) -> StockImportSummary:
    """导一份库存表：商品 upsert、快照 upsert。

    客户不存在抛 `NotFoundError`，文件解析不了抛 `InvalidDataError`。

    🔴 **文件里没出现的 SKU 一律不动，不视为库存归零。** 部分导出是常态（只导
    主推款、只导某个分类），把「这次没导」读成「卖光了」，会让每一次部分导入都
    炸出一屏假告警 —— 而那些告警长得跟真的一模一样。

    **整份导入是幂等的**：同一个 (商品, 时刻) 重传就是覆盖。这条和余额那边的
    409 刻意不同，理由见 `models/product.py` 的 `StockSnapshot` 类 docstring。
    """
    await client_service.get(session, client_id)

    try:
        # 解析是 CPU 密集的，丢进线程池 —— 理由同 `services/imports.py`：在事件
        # 循环里做会把**整个进程**的所有请求一起卡住。
        parsed = await asyncio.to_thread(StockCsvParser().parse, content)
    except ParseError as exc:
        raise InvalidDataError(exc.message) from exc

    existing = {
        product.sku: product
        for product in await session.scalars(
            select(Product).where(
                Product.client_id == client_id,
                Product.sku.in_([row.sku for row in parsed.rows]),
            )
        )
    }

    created = 0
    updated = 0
    for row in parsed.rows:
        product = existing.get(row.sku)
        if product is None:
            product = Product(client_id=client_id, sku=row.sku, name=row.name)
            session.add(product)
            existing[row.sku] = product
            created += 1
            continue
        # 名字跟着文件更新（改名是常事），但**空名字不覆盖已有的** —— 有些导出
        # 只有编码和库存两列，让它把辛苦维护的商品名清空毫无道理。
        if row.name and product.name != row.name:
            product.name = row.name
            updated += 1

    # 商品要先拿到主键，快照才挂得上去。
    await session.flush()

    snapshots = [
        {
            "product_id": existing[row.sku].id,
            "qty": row.qty,
            "daily_sales": row.daily_sales,
            "captured_at": captured_at,
            "note": note,
        }
        for row in parsed.rows
    ]
    statement = insert(StockSnapshot).values(snapshots)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["product_id", "captured_at"],
            set_={
                "qty": statement.excluded.qty,
                "daily_sales": statement.excluded.daily_sales,
                "note": statement.excluded.note,
                # ⚠️ `ON CONFLICT DO UPDATE` 绕过 ORM，`TimestampMixin` 的
                # `onupdate` 在这里不生效 —— 不显式带上就会留下一个停在首次
                # 导入那一刻的 `updated_at`（`models/mixins.py` 点名说了这条）。
                "updated_at": func.now(),
            },
        )
    )

    summary = StockImportSummary(
        client_id=client_id,
        captured_at=captured_at,
        products_created=created,
        products_updated=updated,
        snapshots=len(snapshots),
        skipped_rows=parsed.skipped_rows,
        with_sales_column=sum(1 for row in parsed.rows if row.daily_sales is not None),
    )
    log.info(
        "stock_imported",
        client_id=client_id,
        captured_at=captured_at.isoformat(),
        products_created=created,
        snapshots=summary.snapshots,
        skipped_rows=summary.skipped_rows,
        with_sales_column=summary.with_sales_column,
    )
    return summary


async def list_page(
    session: AsyncSession,
    *,
    client_id: int,
    page: int,
    page_size: int,
    is_active: bool | None = None,
) -> tuple[Sequence[Product], int]:
    """分页列出某客户的商品。客户不存在抛 `NotFoundError`。"""
    await client_service.get(session, client_id)

    filters = [Product.client_id == client_id]
    if is_active is not None:
        filters.append(Product.is_active.is_(is_active))

    total = await session.scalar(select(func.count(Product.id)).where(*filters))
    rows = await session.scalars(
        select(Product)
        .where(*filters)
        .order_by(Product.sku)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def alerts_for_client(
    session: AsyncSession,
    *,
    client_id: int,
    only_alerting: bool = False,
) -> list[StockAlert]:
    """算一个客户所有在售商品的库存状况。

    `client_id` 是**必填关键字参数** —— 客户端那条路径直接用它，而作用域的第二层
    保证就是「查全部客户在这条路径上根本写不出来」（CLAUDE.md 硬规矩 4）。
    """
    client = await client_service.get(session, client_id)
    return await _alerts_for(session, client, only_alerting=only_alerting)


async def alerts(session: AsyncSession, *, only_alerting: bool = True) -> list[StockAlert]:
    """扫一遍**所有有在投账户的客户**的库存。

    🔴 **客户一个在投账户都没有时，跳过它的库存。** 断货告警的全部意义是「广告
    还在跑，货没了」—— 所有账户都停投时，库存不足只是一条店铺经营信息，不是投放
    告警。报出来只会稀释清单，而清单的价值和它的长度成反比（同
    [alerts.md](../../../docs/business/alerts.md) 里「只看两个指标」那条）。

    停用的客户同样不看，理由同余额清单：停止合作的客户库存多少都不重要。
    """
    clients = (
        await session.scalars(
            select(Client)
            .where(
                Client.is_active.is_(True),
                # 有至少一个在投账户。用 EXISTS 而不是 JOIN：JOIN 会让有三个在投
                # 账户的客户出现三次，然后它的每个商品被算三遍。
                select(AdAccount.id)
                .where(AdAccount.client_id == Client.id, AdAccount.is_active.is_(True))
                .exists(),
            )
            .order_by(Client.id)
        )
    ).all()

    found: list[StockAlert] = []
    for client in clients:
        found.extend(await _alerts_for(session, client, only_alerting=only_alerting))

    log.info(
        "stock_alerts_evaluated",
        clients=len(clients),
        alerting=sum(1 for item in found if item.runway.is_alerting),
    )
    return found


async def _alerts_for(
    session: AsyncSession,
    client: Client,
    *,
    only_alerting: bool,
) -> list[StockAlert]:
    products = (
        await session.scalars(
            select(Product)
            .where(Product.client_id == client.id, Product.is_active.is_(True))
            .order_by(Product.sku)
        )
    ).all()

    found: list[StockAlert] = []
    for product in products:
        alert = await _alert(session, client, product)
        if alert is None:
            continue
        if only_alerting and not alert.runway.is_alerting:
            continue
        found.append(alert)

    # 最紧急的排前面。`days_left` 为 None（算不出来）排最后 —— 它不是「很安全」，
    # 只是「不知道」，但它一定不在需要今天处理的那一批里。同 `balance.alerts`。
    found.sort(key=lambda item: (item.runway.days_left is None, item.runway.days_left or 0))
    return found


async def _alert(session: AsyncSession, client: Client, product: Product) -> StockAlert | None:
    """算一个商品的库存状况；一条快照都没有就返回 `None`。

    返回 `None` 而不是造一条「库存 0」的告警：**没导过库存不等于库存是 0**。
    混起来的话每个刚建好的商品都会立刻冒出一条假告警（同 `balance._alert`）。
    """
    points = await _recent_points(session, product.id)
    if not points:
        return None

    latest = points[0]

    # 🔴 优先级写死在这里，不在规则层：规则只收算好的数字，「这个数从哪来」是
    # 查库这一层才知道的事。文件自带的那一列**永远优先** —— 它是店铺后台按真实
    # 订单算的，而推算建立在「中间没补过货」这个假设上。
    if latest.daily_sales is not None:
        avg_daily_sales: Decimal | None = latest.daily_sales
        source = SALES_FROM_FILE
    else:
        avg_daily_sales = stock_rules.infer_daily_sales(
            [
                stock_rules.StockPoint(captured_at=point.captured_at, qty=point.qty)
                for point in points
            ]
        )
        source = SALES_INFERRED if avg_daily_sales is not None else SALES_UNKNOWN

    return StockAlert(
        product_id=product.id,
        client_id=client.id,
        client_name=client.name,
        sku=product.sku,
        name=product.name,
        runway=stock_rules.runway(latest.qty, avg_daily_sales),
        captured_at=latest.captured_at,
        sales_source=source,
        snapshot_count=len(points),
    )


async def _recent_points(session: AsyncSession, product_id: int) -> Sequence[StockSnapshot]:
    """取这个商品最近的几条快照，**最新的在前**。

    按 `captured_at` 排而不是 `created_at`：人可能今天补传昨天导的表，那份的写入
    时间更晚、但它描述的是更早的时刻（同 `balance._latest_balance`）。

    取**条数**而不是天窗口，理由见 `rules/stock.py` 的 `LOOKBACK_POINTS`：库存
    快照的密度由人的导入节奏决定，按天截会让「一周传一次」的客户永远算不出日均。
    """
    rows = await session.scalars(
        select(StockSnapshot)
        .where(StockSnapshot.product_id == product_id)
        .order_by(StockSnapshot.captured_at.desc(), StockSnapshot.id.desc())
        .limit(stock_rules.LOOKBACK_POINTS)
    )
    return rows.all()


async def get(session: AsyncSession, product_id: int) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"商品不存在：{product_id}")
    return product
