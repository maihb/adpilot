"""归一化与日指标查询。

第一条用例就是 **D3–D5 的验收标准**：导一份 CSV 进去，接口能查出归一化日指标。
里程碑写的是「能做到什么」不是「写完了什么代码」，所以这条端到端跑通才算数。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

_CSV = (
    "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,"
    "Link clicks,Results,Purchase conversion value\n"
    "2026-08-18,cmp-1,夏季新品,12.34,1000,50,2,200.00\n"
    "2026-08-19,cmp-1,夏季新品,56.78,2000,80,0,0\n"
).encode()


async def _new_account(api: AsyncClient, suffix: str) -> int:
    client = await api.post("/api/clients", json={"name": f"测试客户-指标-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "meta",
            "external_id": f"acct-metric-{suffix}",
            "name": "测试账户",
            "currency": "USD",
            "timezone": "America/Anchorage",
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


async def _import(
    api: AsyncClient,
    account_id: int,
    content: bytes = _CSV,
    level: str = "campaign",
) -> None:
    response = await api.post(
        "/api/imports",
        files={"file": ("report.csv", content, "text/csv")},
        data={"account_id": str(account_id), "level": level},
    )
    assert response.status_code == 201, response.text


def test_openapi_declares_the_metric_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()
    normalize = schema["paths"]["/api/ad-accounts/{account_id}/normalize"]["post"]
    listing = schema["paths"]["/api/ad-accounts/{account_id}/daily-metrics"]["get"]

    assert normalize["operationId"] == "normalizeAccount"
    assert listing["operationId"] == "listDailyMetrics"
    assert listing["tags"] == ["metrics"]


def test_date_range_is_required(offline_client: TestClient) -> None:
    """日期区间必填。

    这是唯一一张按天线性增长的表，默认全量迟早会把一个看板查询变成全表扫描。
    """
    response = offline_client.get("/api/ad-accounts/1/daily-metrics")

    assert response.status_code == 422


@pytest.mark.integration
async def test_csv_becomes_queryable_metrics(live_api: AsyncClient) -> None:
    """🎯 **D3–D5 的验收标准**：导一份 CSV 进去，接口能查出归一化日指标。"""
    account_id = await _new_account(live_api, "闭环")
    await _import(live_api, account_id)

    normalized = await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    assert normalized.status_code == 200, normalized.text
    assert normalized.json()["rows"] == 2
    assert normalized.json()["days"] == ["2026-08-18", "2026-08-19"]

    listed = await live_api.get(
        f"/api/ad-accounts/{account_id}/daily-metrics",
        params={"start": "2026-08-18", "end": "2026-08-19"},
    )

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 2

    latest = body["items"][0]  # 日期倒序
    assert latest["stat_date"] == "2026-08-19"
    assert latest["object_id"] == "cmp-1"
    assert latest["object_name"] == "夏季新品"
    assert Decimal(latest["spend"]) == Decimal("56.78")
    assert latest["impressions"] == 2000
    # 币种取自账户，不从 "Amount spent (USD)" 那个后缀解析
    assert latest["currency"] == "USD"


@pytest.mark.integration
async def test_derived_metrics_are_computed_not_stored(live_api: AsyncClient) -> None:
    """派生指标现算。公式的真相源是 glossary，这里验的是算得对。"""
    account_id = await _new_account(live_api, "派生")
    await _import(live_api, account_id)
    await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    body = (
        await live_api.get(
            f"/api/ad-accounts/{account_id}/daily-metrics",
            params={"start": "2026-08-18", "end": "2026-08-18"},
        )
    ).json()

    item = body["items"][0]
    assert Decimal(item["cpm"]) == Decimal("12.34")  # 12.34 / 1000 * 1000
    assert Decimal(item["cpc"]) == Decimal("0.2468")  # 12.34 / 50
    assert Decimal(item["ctr"]) == Decimal("0.05")  # 50 / 1000，存小数不存百分数
    assert Decimal(item["cpa"]) == Decimal("6.17")  # 12.34 / 2


@pytest.mark.integration
async def test_zero_denominator_is_null_not_zero(live_api: AsyncClient) -> None:
    """🔴 分母为 0 返回 `null`，**不是 0**。

    写 0 会让「今天没有转化」和「今天 CPA 是 0 元」变成同一个显示值 —— 而这两件
    事在日报里天差地别。8-19 那行的 conversions 正好是 0。
    """
    account_id = await _new_account(live_api, "无定义")
    await _import(live_api, account_id)
    await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    body = (
        await live_api.get(
            f"/api/ad-accounts/{account_id}/daily-metrics",
            params={"start": "2026-08-19", "end": "2026-08-19"},
        )
    ).json()

    item = body["items"][0]
    assert item["cpa"] is None
    assert Decimal(item["roas"]) == 0  # revenue 是 0 但 spend 不是，这个有定义


@pytest.mark.integration
async def test_normalize_is_idempotent(live_api: AsyncClient) -> None:
    """🔴 重跑归一化是**覆盖**不是追加。

    唯一键 `(account_id, level, object_id, stat_date)` 挡住重复。没有它，同一天
    跑两次就是双倍花费 —— 而两次的数字都「对」，只是被加了两遍。
    """
    account_id = await _new_account(live_api, "幂等")
    await _import(live_api, account_id)

    await live_api.post(f"/api/ad-accounts/{account_id}/normalize")
    await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    body = (
        await live_api.get(
            f"/api/ad-accounts/{account_id}/daily-metrics",
            params={"start": "2026-08-18", "end": "2026-08-19"},
        )
    ).json()

    assert body["total"] == 2


@pytest.mark.integration
async def test_latest_snapshot_wins(live_api: AsyncClient) -> None:
    """🔴 同一天有多条快照时，只用 `fetched_at` 最新的那条。

    平台数据在若干天内还会变（归因回传、无效流量剔除、汇率重算），重导是常态。
    旧快照不删 —— 它们回答「当时那个数是多少」——但归一化必须认最新的说法。
    """
    account_id = await _new_account(live_api, "回填")
    await _import(live_api, account_id)

    corrected = (
        "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,Link clicks\n"
        "2026-08-18,cmp-1,夏季新品,99.99,1000,50\n"
    ).encode()
    await _import(live_api, account_id, corrected)
    await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    body = (
        await live_api.get(
            f"/api/ad-accounts/{account_id}/daily-metrics",
            params={"start": "2026-08-18", "end": "2026-08-18"},
        )
    ).json()

    assert Decimal(body["items"][0]["spend"]) == Decimal("99.99")


@pytest.mark.integration
async def test_wrong_level_points_at_the_level(live_api: AsyncClient) -> None:
    """层级填错时，报错要**指向层级**而不是只说找不到列。

    这是导入时最容易填错的一个参数，而它是唯一键的一部分 —— 填错了不会覆盖旧行，
    会新增一份，于是同一天的花费在汇总时被算两遍。
    """
    account_id = await _new_account(live_api, "错层级")
    await _import(live_api, account_id, level="ad")  # 文件里只有 Campaign ID

    response = await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "层级" in detail
    assert "Campaign ID" in detail  # 要把实际表头列出来


@pytest.mark.integration
async def test_rows_without_object_id_are_skipped(live_api: AsyncClient) -> None:
    """没有对象 ID 的行进不了唯一键，跳过并计数。"""
    account_id = await _new_account(live_api, "缺ID")
    await _import(
        live_api,
        account_id,
        (
            b"Day,Campaign ID,Amount spent (USD)\n"
            b"2026-08-18,cmp-1,10\n"
            b"2026-08-18,,999\n"  # 小计行，没有对象 ID
        ),
    )

    normalized = await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    assert normalized.json()["rows"] == 1
    assert normalized.json()["skipped_rows"] == 1


@pytest.mark.integration
async def test_normalize_without_snapshots_is_not_an_error(live_api: AsyncClient) -> None:
    """还没导入就跑归一化，返回 0 行而不是报错 —— 那不是错误状态。"""
    account_id = await _new_account(live_api, "空跑")

    response = await live_api.post(f"/api/ad-accounts/{account_id}/normalize")

    assert response.status_code == 200
    assert response.json()["rows"] == 0
    assert response.json()["snapshots"] == 0


@pytest.mark.integration
async def test_unknown_account_is_404_on_both_endpoints(live_api: AsyncClient) -> None:
    """账户不存在要 404，而不是返回空列表 —— 否则前端分不出「查无此账户」和
    「这段时间没数据」，只能靠猜给提示。"""
    assert (await live_api.post("/api/ad-accounts/999999/normalize")).status_code == 404

    listed = await live_api.get(
        "/api/ad-accounts/999999/daily-metrics",
        params={"start": "2026-08-18", "end": "2026-08-19"},
    )
    assert listed.status_code == 404
