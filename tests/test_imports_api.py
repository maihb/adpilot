"""导入接口与原始快照落盘。

集成那几条验的是**只有真连上两个库才知道**的事：快照确实按天落进了 Mongo、
payload 一个字段都没被改过、以及同一天导两次得到的是两条快照而不是一条。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from adpilot.db.mongo import RAW_REPORTS, MongoDatabase

_CSV = b"Day,Campaign,Amount spent (USD)\n2026-08-18,cmp-1,12.34\n2026-08-19,cmp-1,56.78\n"


async def _new_account(api: AsyncClient, suffix: str) -> int:
    client = await api.post("/api/clients", json={"name": f"测试客户-导入-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "tiktok",
            "external_id": f"acct-import-{suffix}",
            "name": "测试账户",
            "currency": "USD",
            "timezone": "America/Anchorage",
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


def _files(content: bytes = _CSV) -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("report.csv", content, "text/csv")}


def _form(account_id: int, **extra: str) -> dict[str, str]:
    return {"account_id": str(account_id), **extra}


def test_openapi_declares_the_import_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/imports"]["post"]["operationId"] == "importReportFile"
    assert schema["paths"]["/api/imports/providers"]["get"]["operationId"] == "listImportProviders"
    assert schema["paths"]["/api/imports"]["post"]["tags"] == ["imports"]


def test_provider_list_is_served_from_the_registry(offline_client: TestClient) -> None:
    """前端的下拉框要从这里取，硬编码一份清单的话接新平台会漏改。"""
    response = offline_client.get("/api/imports/providers")

    assert response.status_code == 200
    assert "file_csv" in response.json()


@pytest.mark.integration
async def test_import_lands_one_snapshot_per_day(
    live_api: AsyncClient,
    live_mongo: MongoDatabase,
) -> None:
    """两天的数据要落成两条快照，摘要里的天数和行数要对得上。"""
    account_id = await _new_account(live_api, "落盘")

    response = await live_api.post("/api/imports", files=_files(), data=_form(account_id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider"] == "file_csv"
    assert body["days"] == ["2026-08-18", "2026-08-19"]
    assert body["rows"] == 2
    assert body["skipped_rows"] == 0

    stored = await live_mongo[RAW_REPORTS].count_documents({"account_id": account_id})
    assert stored == 2


@pytest.mark.integration
async def test_payload_is_stored_verbatim(
    live_api: AsyncClient,
    live_mongo: MongoDatabase,
) -> None:
    """🔴 落进 Mongo 的必须是**未经解释的原始行**。

    列名提前改过的话，这份快照就永久带上了当时的映射规则，重跑归一化也救不回来
    —— 而重跑正是养这第二个库的全部理由。
    """
    account_id = await _new_account(live_api, "原样")

    await live_api.post("/api/imports", files=_files(), data=_form(account_id))

    doc = await live_mongo[RAW_REPORTS].find_one(
        {"account_id": account_id, "stat_date": "2026-08-18"}
    )
    assert doc is not None
    assert doc["provider"] == "file_csv"
    # stat_date 存的是 ISO 字符串不是 datetime：它是账户时区下的自然日、不是时刻
    assert doc["stat_date"] == "2026-08-18"
    assert doc["payload"] == [
        {"Day": "2026-08-18", "Campaign": "cmp-1", "Amount spent (USD)": "12.34"}
    ]


@pytest.mark.integration
async def test_reimport_appends_instead_of_replacing(
    live_api: AsyncClient,
    live_mongo: MongoDatabase,
) -> None:
    """🔴 同一天导两次 = 两条快照，这是**刻意的**。

    平台数据在若干天内还会变（归因回传、无效流量剔除、汇率重算），而「当时这个数
    到底是多少」只能从快照里查 —— 覆盖掉旧的就等于把那个答案抹了。去重是归一化
    按唯一键 upsert 时的事，不是这一层的事。
    """
    account_id = await _new_account(live_api, "重导")

    await live_api.post("/api/imports", files=_files(), data=_form(account_id))
    second = await live_api.post("/api/imports", files=_files(), data=_form(account_id))

    assert second.status_code == 201
    stored = await live_mongo[RAW_REPORTS].count_documents(
        {"account_id": account_id, "stat_date": "2026-08-18"}
    )
    assert stored == 2


@pytest.mark.integration
async def test_unknown_account_is_rejected(live_api: AsyncClient) -> None:
    """快照的 account_id 是它唯一的归属标记。

    落一条指向不存在账户的快照，等于造了一份**永远不会被归一化**的数据，而它
    看起来跟正常快照毫无区别。
    """
    response = await live_api.post("/api/imports", files=_files(), data=_form(999999))

    assert response.status_code == 422
    assert "广告账户不存在" in response.json()["detail"]


@pytest.mark.integration
async def test_unparsable_file_reports_where_it_broke(live_api: AsyncClient) -> None:
    """解析失败要 422，且 detail 能指导排查 —— 几千行的文件里「解析失败」等于没说。"""
    account_id = await _new_account(live_api, "坏文件")

    response = await live_api.post(
        "/api/imports",
        files=_files(b"Campaign,Spend\ncmp-1,12.34\n"),
        data=_form(account_id),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Campaign" in detail  # 报错要带上实际表头


@pytest.mark.integration
async def test_unknown_provider_is_rejected(live_api: AsyncClient) -> None:
    account_id = await _new_account(live_api, "坏源")

    response = await live_api.post(
        "/api/imports",
        files={"file": ("report.csv", _CSV, "text/csv")},
        data={"account_id": str(account_id), "provider": "meta_api"},
    )

    assert response.status_code == 422
    assert "file_csv" in response.json()["detail"]  # 要把可选值列出来


@pytest.mark.integration
async def test_total_row_is_reported_not_hidden(
    live_api: AsyncClient,
    live_mongo: MongoDatabase,
) -> None:
    """末尾的 Total 行被跳过，但要在响应里报出来。"""
    account_id = await _new_account(live_api, "汇总行")

    response = await live_api.post(
        "/api/imports",
        files=_files(b"Day,Campaign,Spend\n2026-08-18,cmp-1,10\n,Total,100\n"),
        data=_form(account_id),
    )

    assert response.status_code == 201
    assert response.json()["skipped_rows"] == 1
    assert response.json()["rows"] == 1
