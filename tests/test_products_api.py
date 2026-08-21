"""商品、库存导入与断货预警。

集成那几条验的是**只有真连上数据库才知道**的事：一份 CSV 怎么落成「商品 + 快照」、
重传是不是真的幂等、日均在「文件带了那一列」和「只能靠推」两种情况下走的是不是
不同的路，以及**客户级告警端到端能不能触发** —— 最后这条是 D16 的验收标准。

规则本身的边界（分母为 0、卖光了、阈值含不含等号、补货段怎么跳）在
`test_rules_stock.py` 里用纯函数覆盖；列名认不认得出在 `test_stock_csv.py` 里。
这里都不重复。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient


async def _new_client(api: AsyncClient, suffix: str) -> int:
    created = await api.post("/api/clients", json={"name": f"测试客户-库存-{suffix}"})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


async def _new_account(api: AsyncClient, client_id: int, suffix: str) -> int:
    """给客户配一个在投账户。

    **不是可有可无的准备工作**：库存巡检只看「有在投账户」的客户，因为断货告警的
    意义是「广告还在跑，货没了」。没有这一步，下面那些用例会全部得到空清单。
    """
    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client_id,
            "platform": "tiktok",
            "external_id": f"acct-stock-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": "America/Anchorage",
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


async def _import(
    api: AsyncClient,
    client_id: int,
    csv: str,
    *,
    days_ago: float = 0,
) -> dict[str, object]:
    captured = datetime.now(UTC) - timedelta(days=days_ago)
    response = await api.post(
        f"/api/clients/{client_id}/stock-imports",
        files={"file": ("stock.csv", csv.encode(), "text/csv")},
        data={"captured_at": captured.isoformat()},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _runway(api: AsyncClient, client_id: int) -> list[dict[str, object]]:
    response = await api.get(f"/api/clients/{client_id}/stock-runway")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_openapi_declares_the_stock_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    imported = schema["paths"]["/api/clients/{client_id}/stock-imports"]["post"]
    products = schema["paths"]["/api/clients/{client_id}/products"]["get"]
    runway = schema["paths"]["/api/clients/{client_id}/stock-runway"]["get"]

    assert imported["operationId"] == "importStock"
    assert products["operationId"] == "listProducts"
    assert runway["operationId"] == "listStockRunway"
    assert imported["tags"] == ["products"]


def test_naive_timestamp_is_rejected(offline_client: TestClient) -> None:
    """不带时区的 `captured_at` 要 422，理由同余额那条。

    这里比余额更要紧一点：推算日均按**时刻差**算，`captured_at` 偏几小时就直接
    落进那个除法的分母。这条不需要数据库 —— 校验发生在服务层之前。
    """
    response = offline_client.post(
        "/api/clients/1/stock-imports",
        files={"file": ("stock.csv", b"sku,stock\nA-001,10\n", "text/csv")},
        data={"captured_at": "2026-08-21T10:00:00"},
    )
    assert response.status_code == 422


@pytest.mark.integration
async def test_import_creates_products_and_snapshots(live_api: AsyncClient) -> None:
    client_id = await _new_client(live_api, "首次导入")

    summary = await _import(
        live_api,
        client_id,
        "sku,商品名称,库存,日均销量\nA-001,夏季连衣裙,120,8\nA-002,针织开衫,40,10\n",
    )

    assert summary["products_created"] == 2
    assert summary["snapshots"] == 2
    assert summary["with_sales_column"] == 2

    listed = await live_api.get(f"/api/clients/{client_id}/products")
    assert [item["sku"] for item in listed.json()["items"]] == ["A-001", "A-002"]


@pytest.mark.integration
async def test_reimporting_the_same_instant_overwrites(live_api: AsyncClient) -> None:
    """🔴 重传同一份是**覆盖**，不是 409 —— 这是库存和余额刻意不同的一条。

    余额是人手敲的，同一个 (账户, 时刻) 出现两次基本是误操作；库存是文件上传，
    重传（网络断了、发现少了一列重来）是常态。判成冲突等于逼人先去删数据。
    """
    client_id = await _new_client(live_api, "重传")
    captured = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    for stock in ("120", "90"):
        response = await live_api.post(
            f"/api/clients/{client_id}/stock-imports",
            files={"file": ("stock.csv", f"sku,库存\nA-001,{stock}\n".encode(), "text/csv")},
            data={"captured_at": captured},
        )
        assert response.status_code == 201, response.text

    # 商品只有一个（第二次是 upsert 不是新建），库存是后写的那个值。
    products = (await live_api.get(f"/api/clients/{client_id}/products")).json()
    assert products["total"] == 1

    await _new_account(live_api, client_id, "重传")
    items = await _runway(live_api, client_id)
    assert len(items) == 1
    assert items[0]["stock_qty"] == "90.0000"


@pytest.mark.integration
async def test_skus_missing_from_the_file_are_left_alone(live_api: AsyncClient) -> None:
    """🔴 文件里没出现的 SKU 不动，**不视为库存归零**。

    只导主推款、只导某个分类都是常态。把「这次没导」读成「卖光了」，会让每次部分
    导入都炸出一屏假告警 —— 而那些告警长得跟真的一模一样。
    """
    client_id = await _new_client(live_api, "部分导出")
    await _new_account(live_api, client_id, "部分导出")

    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,120,4\nA-002,300,4\n", days_ago=1)
    # 第二次只导 A-001。A-002 应当保持第一次那条快照，而不是变成 0。
    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,100,4\n")

    by_sku = {item["sku"]: item for item in await _runway(live_api, client_id)}
    assert by_sku["A-001"]["stock_qty"] == "100.0000"
    assert by_sku["A-002"]["stock_qty"] == "300.0000"
    assert by_sku["A-002"]["is_alerting"] is False


@pytest.mark.integration
async def test_daily_sales_from_the_file_wins(live_api: AsyncClient) -> None:
    """文件自带那一列时用它，`sales_source` 说清这一点。

    出参里带着来源不是装饰：推算出来的日均建立在「中间没补过货」这个假设上，
    而人看到「还能撑 2 天」时第一个该问的就是这个数字可信不可信。
    """
    client_id = await _new_client(live_api, "销量列")
    await _new_account(live_api, client_id, "销量列")

    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,120,8\n")

    item = (await _runway(live_api, client_id))[0]
    assert item["sales_source"] == "file"
    assert item["avg_daily_sales"] == "8.0000"
    assert item["days_left"] == "15.0"
    assert item["is_alerting"] is False


@pytest.mark.integration
async def test_daily_sales_is_inferred_from_two_imports(live_api: AsyncClient) -> None:
    """没有销量列时，日均从两次导入之间的库存变化推出来。

    第一次导完 `snapshot_count` 是 1 —— 那时**一定**算不出日均，页面据此提示
    「再导一次就能算了」，而不是笼统的「算不出来」。
    """
    client_id = await _new_client(live_api, "推算")
    await _new_account(live_api, client_id, "推算")

    await _import(live_api, client_id, "sku,库存\nA-001,100\n", days_ago=4)
    first = (await _runway(live_api, client_id))[0]
    assert first["snapshot_count"] == 1
    assert first["sales_source"] == "none"
    assert first["days_left"] is None

    await _import(live_api, client_id, "sku,库存\nA-001,60\n")
    second = (await _runway(live_api, client_id))[0]
    assert second["sales_source"] == "inferred"
    # 4 天掉了 40 → 日均 10 → 60 件还能撑 6 天，低于 7 天阈值。
    assert second["avg_daily_sales"] == "10.0000"
    assert second["days_left"] == "6.0"
    assert second["is_alerting"] is True


@pytest.mark.integration
async def test_products_without_snapshots_never_appear(live_api: AsyncClient) -> None:
    """没有任何快照的商品不出现在清单里 —— 没导过库存 ≠ 库存是 0。

    混起来的话每个刚建好的商品都会立刻显示成「已断货」，而假告警会让人对整个
    清单脱敏（同 `balance._alert` 那条）。
    """
    client_id = await _new_client(live_api, "无快照")
    await _new_account(live_api, client_id, "无快照")

    assert await _runway(live_api, client_id) == []


@pytest.mark.integration
async def test_stock_alert_is_client_level_and_deduped(live_api: AsyncClient) -> None:
    """🔴 端到端：断货告警开出来、`account_id` 是 null、巡检十次也只有一条。

    去重这条特别值得钉：客户级告警的 `account_id` 是 NULL，而 **PostgreSQL 的
    唯一索引里 NULL 不等于 NULL** —— 少了那个专门的部分索引，巡检每小时都会新开
    一条一模一样的，一天二十四条，而每一条看起来都对。
    """
    client_id = await _new_client(live_api, "端到端")
    await _new_account(live_api, client_id, "端到端")
    await _import(live_api, client_id, "sku,商品名称,库存,日均销量\nA-001,主推款,10,5\n")

    for _ in range(3):
        swept = await live_api.post("/api/alerts/sweep")
        assert swept.status_code == 200, swept.text

    listed = await live_api.get("/api/alerts", params={"status": "open"})
    mine = [
        item
        for item in listed.json()["items"]
        if item["client_id"] == client_id and item["kind"] == "stock_low"
    ]

    assert len(mine) == 1, "同一件事巡检三次开出了不止一条"
    assert mine[0]["account_id"] is None, "库存告警不该指向任何账户"
    assert mine[0]["subject"] == "stock:A-001"
    assert "主推款" in mine[0]["message"]
    assert mine[0]["detail"]["sales_source"] == "file"


@pytest.mark.integration
async def test_restocking_resolves_the_alert(live_api: AsyncClient) -> None:
    """补了货就自动收掉 —— 不用人去点。

    这条盯的是对账那一趟遍历的是**被巡检过的客户**而不是「有断货商品的客户」：
    后者会让补货之后那条告警永远留在清单上，因为它不再出现在 findings 里。
    """
    client_id = await _new_client(live_api, "补货")
    await _new_account(live_api, client_id, "补货")

    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,10,5\n", days_ago=1)
    await live_api.post("/api/alerts/sweep")

    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,500,5\n")
    await live_api.post("/api/alerts/sweep")

    listed = await live_api.get("/api/alerts")
    mine = [
        item
        for item in listed.json()["items"]
        if item["client_id"] == client_id and item["kind"] == "stock_low"
    ]
    assert len(mine) == 1
    assert mine[0]["status"] == "resolved"
    assert mine[0]["resolved_at"] is not None


@pytest.mark.integration
async def test_clients_without_live_accounts_are_skipped(live_api: AsyncClient) -> None:
    """🔴 一个在投账户都没有的客户，库存不巡检。

    断货告警的全部意义是「广告还在跑，货没了」。所有账户都停投时，库存不足只是
    一条店铺经营信息 —— 报出来只会稀释清单，而清单的价值和它的长度成反比。
    """
    client_id = await _new_client(live_api, "无在投")
    account_id = await _new_account(live_api, client_id, "无在投")
    await live_api.patch(f"/api/ad-accounts/{account_id}", json={"is_active": False})

    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,1,5\n")
    await live_api.post("/api/alerts/sweep")

    listed = await live_api.get("/api/alerts", params={"status": "open"})
    mine = [item for item in listed.json()["items"] if item["client_id"] == client_id]
    assert mine == []


@pytest.mark.integration
async def test_the_client_portal_sees_its_stock_alert(live_api: AsyncClient) -> None:
    """🔴 客户端看得到自己的断货告警。

    这条盯的是 `list_for_client` 那次 JOIN 的移除：告警挂在客户上、`account_id`
    是 NULL，而**内连接会把它整个筛掉** —— 客户于是永远看不到自己的断货告警，
    并且不会有任何报错。
    """
    client_id = await _new_client(live_api, "客户端可见")
    await _new_account(live_api, client_id, "客户端可见")
    await _import(live_api, client_id, "sku,库存,日均销量\nA-001,10,5\n")
    await live_api.post("/api/alerts/sweep")

    code = (await live_api.post(f"/api/clients/{client_id}/invites", json={})).json()["code"]
    token = (await live_api.post("/api/auth/redeem", json={"code": code})).json()["token"]

    listed = await live_api.get(
        "/api/portal/alerts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200, listed.text
    kinds = [item["kind"] for item in listed.json()["items"]]
    assert "stock_low" in kinds
