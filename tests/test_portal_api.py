"""客户端接口：作用域是不是真的锁死了。

D9 的验收标准只有一句 —— **拿别人的 token 查不到我的数据，且这件事有测试盯着**。
这个文件就是那句话的机器形态：每一个客户端接口都被拿 B 客户的 token 打一遍 A
客户的数据。

漏一次的后果是把 A 的花费给 B 看见，而这种漏**不会有任何报错**，也不会有人来
投诉。所以越权用例比成功用例更重要。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from adpilot.models.ad_account import Platform


def test_openapi_declares_stable_operation_ids(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/portal/me"]["get"]["operationId"] == "getPortalProfile"
    assert schema["paths"]["/api/portal/accounts"]["get"]["operationId"] == "listPortalAccounts"
    assert schema["paths"]["/api/portal/alerts"]["get"]["operationId"] == "listPortalAlerts"


def test_no_portal_endpoint_takes_a_client_id(offline_client: TestClient) -> None:
    """**客户端接口不接受任何形式的 `client_id` 入参。**

    它只能来自 token。这条守的是「哪天有人为了方便加了个查询参数」—— 那一下就
    把整个作用域机制变成了摆设，而且不会有任何报错。
    """
    schema = offline_client.get("/openapi.json").json()

    for path, operations in schema["paths"].items():
        if "/api/portal/" not in path:
            continue
        assert "client_id" not in path, f"{path} 的路径里带了 client_id"
        for method, operation in operations.items():
            names = {p["name"] for p in operation.get("parameters", [])}
            assert "client_id" not in names, f"{method} {path} 收了一个 client_id 参数"


def test_portal_alerts_do_not_expose_the_notification_timestamp(
    offline_client: TestClient,
) -> None:
    """客户端的告警出参比内部那套少一个 `notified_at`。

    推送成功没有是运维信息，跟客户无关。这条同时是「两套 response model」这个
    决定的锚点：哪天有人把它们合并了，这里会红。
    """
    schemas = offline_client.get("/openapi.json").json()["components"]["schemas"]

    assert "notified_at" in schemas["AlertItem"]["properties"]
    assert "notified_at" not in schemas["PortalAlertItem"]["properties"]


async def _client_with_account(api: AsyncClient, name: str, external_id: str) -> tuple[int, int]:
    """建一个客户 + 一个广告账户，返回 (client_id, account_id)。"""
    client_id = (await api.post("/api/clients", json={"name": name})).json()["id"]
    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client_id,
            "platform": Platform.META.value,
            "external_id": external_id,
            "name": f"{name}-账户",
            "currency": "USD",
            "timezone": "America/Los_Angeles",
        },
    )
    assert account.status_code == 201, account.text
    return client_id, account.json()["id"]


async def _token_for(api: AsyncClient, client_id: int) -> str:
    """给一个客户生成邀请码并换成客户端 token。"""
    code = (await api.post(f"/api/clients/{client_id}/invites", json={})).json()["code"]
    return str((await api.post("/api/auth/redeem", json={"code": code})).json()["token"])


@pytest.mark.integration
async def test_a_client_sees_only_their_own_accounts(live_api: AsyncClient) -> None:
    mine, my_account = await _client_with_account(live_api, "测试客户-作用域-我", "demo-scope-a")
    theirs, their_account = await _client_with_account(
        live_api, "测试客户-作用域-别人", "demo-scope-b"
    )
    headers = {"Authorization": f"Bearer {await _token_for(live_api, mine)}"}

    listed = await live_api.get("/api/portal/accounts", headers=headers)

    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert ids == [my_account]
    assert their_account not in ids
    assert theirs != mine


@pytest.mark.integration
async def test_the_profile_hides_the_internal_note(live_api: AsyncClient) -> None:
    """内部备注（「这家结算总拖」之类）绝不能出现在客户那一侧。"""
    created = await live_api.post(
        "/api/clients",
        json={"name": "测试客户-作用域-备注", "note": "内部备注不该外泄"},
    )
    client_id = created.json()["id"]
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}

    me = await live_api.get("/api/portal/me", headers=headers)

    assert me.status_code == 200
    assert me.json() == {"id": client_id, "name": "测试客户-作用域-备注"}


@pytest.mark.integration
async def test_another_clients_account_is_a_404_everywhere(live_api: AsyncClient) -> None:
    """🔴 **D9 的验收标准本身。**

    拿 B 的 token 去打 A 的 `account_id`，每一个带 account_id 的客户端接口都必须
    404 —— 不是 403（那等于承认这个账户存在），更不是 200。
    """
    _, a_account = await _client_with_account(live_api, "测试客户-越权-A", "demo-cross-a")
    b_client, _ = await _client_with_account(live_api, "测试客户-越权-B", "demo-cross-b")
    b_headers = {"Authorization": f"Bearer {await _token_for(live_api, b_client)}"}

    today = date.today()
    scoped_paths = [
        f"/api/portal/accounts/{a_account}/daily-metrics"
        f"?start={today - timedelta(days=7)}&end={today}",
        f"/api/portal/accounts/{a_account}/balance-runway",
    ]

    for path in scoped_paths:
        response = await live_api.get(path, headers=b_headers)
        assert response.status_code == 404, f"{path} 让 B 看到了 A 的数据：{response.status_code}"


@pytest.mark.integration
async def test_metrics_come_back_with_the_currency_and_timezone(live_api: AsyncClient) -> None:
    """时间线必须带口径。

    不注明的话，客户拿他自己后台的数字来对，永远差一截 —— 而那是解释不清的差异，
    不是数据错误。
    """
    client_id, account_id = await _client_with_account(
        live_api, "测试客户-作用域-口径", "demo-scope-tz"
    )
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}
    today = date.today()

    response = await live_api.get(
        f"/api/portal/accounts/{account_id}/daily-metrics",
        params={"start": str(today - timedelta(days=7)), "end": str(today)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currency"] == "USD"
    assert body["timezone"] == "America/Los_Angeles"
    assert body["items"] == [], "这个账户还没有数据，不该凭空补出几天来"


@pytest.mark.integration
async def test_the_range_is_capped(live_api: AsyncClient) -> None:
    """「把 start 填成 2020-01-01」那种手滑要被拦住，而不是扫出几千行。"""
    client_id, account_id = await _client_with_account(
        live_api, "测试客户-作用域-区间", "demo-scope-range"
    )
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}

    response = await live_api.get(
        f"/api/portal/accounts/{account_id}/daily-metrics",
        params={"start": "2020-01-01", "end": str(date.today())},
        headers=headers,
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_runway_says_it_does_not_know_rather_than_zero(live_api: AsyncClient) -> None:
    """从没录过余额时各字段是 `null`。

    造一个「余额 0」出来，会让每个刚建好的账户看起来都在着火 —— 而假告警会让人
    对整个页面脱敏，那比没有这个数字更糟。
    """
    client_id, account_id = await _client_with_account(
        live_api, "测试客户-作用域-余额", "demo-scope-runway"
    )
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}

    response = await live_api.get(
        f"/api/portal/accounts/{account_id}/balance-runway", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is None
    assert body["days_left"] is None
    assert body["is_alerting"] is False


@pytest.mark.integration
async def test_runway_reports_a_real_balance(live_api: AsyncClient) -> None:
    client_id, account_id = await _client_with_account(
        live_api, "测试客户-作用域-余额有值", "demo-scope-runway2"
    )
    recorded = await live_api.post(
        f"/api/ad-accounts/{account_id}/balances",
        json={
            "available": "1234.5600",
            "captured_at": datetime.now(UTC).isoformat(),
        },
    )
    assert recorded.status_code == 201, recorded.text
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}

    response = await live_api.get(
        f"/api/portal/accounts/{account_id}/balance-runway", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["available"]) == Decimal("1234.5600")
    assert body["currency"] == "USD"


@pytest.mark.integration
async def test_alerts_are_scoped_to_the_client(live_api: AsyncClient) -> None:
    """告警列表按客户过滤，且不接受 `account_id` —— 客户要的是「我这边怎么样」。"""
    client_id, _ = await _client_with_account(live_api, "测试客户-作用域-告警", "demo-scope-alerts")
    headers = {"Authorization": f"Bearer {await _token_for(live_api, client_id)}"}

    response = await live_api.get("/api/portal/alerts", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0}


@pytest.mark.integration
async def test_an_operator_token_cannot_call_the_portal(live_api: AsyncClient) -> None:
    """反方向的越权：运营 token 打客户端接口要 401。

    运营 token 里没有 `client_id`，放行的话这些接口就成了「过滤不了的全量查询」。
    """
    assert (await live_api.get("/api/portal/me")).status_code == 401
