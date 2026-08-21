"""商品与库存的路由：导进来、查回去、算这个款还能撑几天。

**跨客户的断货清单不在这里**，它在 `api/alert.py` —— 所有 `/alerts*` 归一处，
同 `api/balance.py` 那条：不然 OpenAPI 分组时同一个路径前缀会散在两个 tag 下。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from adpilot.api.deps import SessionDep
from adpilot.api.errors import responses
from adpilot.api.pagination import DEFAULT_PAGE_SIZE, PageParam, PageSizeParam
from adpilot.schemas.product import (
    ProductItem,
    ProductListResponse,
    StockImportResponse,
    StockRunwayListResponse,
    StockRunwayResponse,
)
from adpilot.services import product as product_service

router = APIRouter(tags=["products"])

# 上限的理由同 `api/imports.py`：UploadFile 会把整个文件读进内存。库存表比报表
# 小得多（一个客户几百个 SKU 撑死几十 KB），2 MiB 已经很宽松了。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@router.post(
    "/clients/{client_id}/stock-imports",
    response_model=StockImportResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="importStock",
    responses=responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def import_stock(
    client_id: int,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="店铺后台导出的库存表（CSV）")],
    captured_at: Annotated[
        datetime | None,
        Form(description="这批库存属于哪一刻，必须带时区。不给就取此刻"),
    ] = None,
    note: Annotated[
        str | None,
        Form(description="从哪导的、什么口径。会记在这一批的每条快照上"),
    ] = None,
) -> StockImportResponse:
    """导一份库存表：商品 upsert、快照 upsert。

    **只有批量，没有单条录入** —— 和余额刚好相反。余额一个账户一个数，手工录合理；
    库存一个客户几十上百个 SKU，手工录不现实。

    列名自动认（`sku`/`商品编码`、`库存`/`stock`、可选的 `日均销量`），认不出必填
    列会 422 并把表头列出来。**日均销量那一列是可选的**：没有它时日均由库存变化
    推算，而那需要**至少两次导入**才有结果。

    🔴 **文件里没出现的 SKU 一律不动，不视为库存归零。** 只导主推款、只导某个分类
    都是常态，把「这次没导」读成「卖光了」会让每次部分导入都炸出一屏假告警。

    **整份导入是幂等的**：同一个 (商品, 时刻) 重传就是覆盖，不像余额那样回 409 ——
    库存是文件上传，重传同一份（网络断了、少了一列重来）是常态而不是误操作。
    """
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MiB 上限",
        )
    if captured_at is not None and captured_at.tzinfo is None:
        # 同 `BalanceCreateRequest` 那条校验：放行的话它会被当成服务器本地时区，
        # 而服务器时区和店铺时区常常不是一回事 —— 偏几小时不会让任何东西报错，
        # 只会让「这份库存是什么时候的」说不清，而推算日均正是按时刻差算的。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="captured_at 必须带时区，例如 2026-08-21T10:00:00+08:00",
        )

    summary = await product_service.import_stock(
        session,
        client_id=client_id,
        content=await file.read(),
        captured_at=captured_at or datetime.now(UTC),
        note=note,
    )
    return StockImportResponse.model_validate(summary)


@router.get(
    "/clients/{client_id}/products",
    response_model=ProductListResponse,
    operation_id="listProducts",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_products(
    client_id: int,
    session: SessionDep,
    page: PageParam = 1,
    page_size: PageSizeParam = DEFAULT_PAGE_SIZE,
    is_active: bool | None = None,
) -> ProductListResponse:
    """列出某客户的商品，按编码排序。"""
    rows, total = await product_service.list_page(
        session,
        client_id=client_id,
        page=page,
        page_size=page_size,
        is_active=is_active,
    )
    return ProductListResponse(
        items=[ProductItem.model_validate(row) for row in rows],
        total=total,
    )


@router.get(
    "/clients/{client_id}/stock-runway",
    response_model=StockRunwayListResponse,
    operation_id="listStockRunway",
    responses=responses(status.HTTP_404_NOT_FOUND),
)
async def list_stock_runway(
    client_id: int,
    session: SessionDep,
    only_alerting: bool = False,
) -> StockRunwayListResponse:
    """这个客户每个在售商品还能撑几天，最紧急的在前。

    **默认给全部在售商品**（不只是告警的那些）：运营打开这一屏是来看整体的，
    而「哪些算不出来」恰恰是要看见的信息 —— 那意味着还得再导一次库存。

    没有任何快照的商品**不出现在这里**：没导过库存不等于库存是 0，混起来会让每个
    刚建好的商品立刻显示成「已断货」。
    """
    alerts = await product_service.alerts_for_client(
        session,
        client_id=client_id,
        only_alerting=only_alerting,
    )
    items = [to_stock_response(alert) for alert in alerts]
    return StockRunwayListResponse(items=items, total=len(items))


def to_stock_response(alert: product_service.StockAlert) -> StockRunwayResponse:
    """把服务层的结果摊平成出参。

    摊平而不是嵌套一层 `runway`，理由同 `api/balance.py` 的 `to_runway_response`：
    调用方要的是一行能直接显示的东西。放在这个模块也是同一个理由 —— 它认识
    `services` 里的类型，而分层契约里 `schemas` 在 `services` **之下**。
    """
    return StockRunwayResponse(
        product_id=alert.product_id,
        client_id=alert.client_id,
        client_name=alert.client_name,
        sku=alert.sku,
        name=alert.name,
        stock_qty=alert.runway.stock_qty,
        avg_daily_sales=alert.runway.avg_daily_sales,
        days_left=alert.runway.days_left,
        is_alerting=alert.runway.is_alerting,
        threshold_days=alert.runway.threshold_days,
        sales_source=alert.sales_source,
        captured_at=alert.captured_at,
        snapshot_count=alert.snapshot_count,
    )
