"""广告账户接口的测试。

这个领域的测试重心与客户不同：账户上那三个字段（`timezone` / `currency` /
`external_id`）决定了后面所有指标怎么解释，**写错了数据看起来完全正常**，所以
校验和唯一性是这里最值得钉死的部分。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient


def _payload(client_id: int, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "client_id": client_id,
        "platform": "tiktok",
        "external_id": "acct-1001",
        "name": "测试账户",
        "currency": "USD",
        # 真实案例里的日切点差异就来自这种时区，不是随手挑的
        "timezone": "America/Anchorage",
    }
    return base | overrides


async def _new_client(live_api: AsyncClient, name: str) -> int:
    response = await live_api.post("/api/clients", json={"name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_bad_timezone_is_rejected(offline_client: TestClient) -> None:
    """🔴 拼错的时区名必须在入口就被拦下。

    `timezone` 是 `stat_date` 的口径依据。写错一个字母，这个账户所有日期的日切点
    就是错的 —— 而数据看起来毫无异常，只有客户拿他自己后台的数字来对时才会发现
    差一截，那时已经积累了几周的错数据，还得重跑归一化才能修。
    """
    bad = offline_client.post(
        "/api/ad-accounts",
        json=_payload(1, timezone="Asia/Shanghai_typo"),
    )
    assert bad.status_code == 422

    # UTC 偏移量也不行：夏令时切换那天它是错的，而广告数据恰恰按自然日切
    assert (
        offline_client.post("/api/ad-accounts", json=_payload(1, timezone="+08:00")).status_code
        == 422
    )


def test_currency_must_be_upper_case_iso_4217(offline_client: TestClient) -> None:
    """小写币种一律拒掉，**不悄悄转成大写**。

    悄悄转换看着体贴，代价是同一个账户在库里可能存过 "usd" 也存过 "USD"，跨账户
    汇总时按币种分组就会分裂成两组，而两组数字都对、加起来才是全部。
    """
    assert (
        offline_client.post("/api/ad-accounts", json=_payload(1, currency="usd")).status_code == 422
    )
    assert (
        offline_client.post("/api/ad-accounts", json=_payload(1, currency="US")).status_code == 422
    )
    assert (
        offline_client.post("/api/ad-accounts", json=_payload(1, currency="DOLLAR")).status_code
        == 422
    )


def test_unknown_platform_is_rejected(offline_client: TestClient) -> None:
    """平台是枚举，没接的平台传进来要 422 而不是存成一个谁也不认识的字符串。"""
    response = offline_client.post("/api/ad-accounts", json=_payload(1, platform="google"))

    assert response.status_code == 422


def test_openapi_declares_stable_operation_ids(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/ad-accounts"]["post"]["operationId"] == "createAdAccount"
    assert schema["paths"]["/api/ad-accounts"]["get"]["operationId"] == "listAdAccounts"
    assert schema["paths"]["/api/ad-accounts/{account_id}"]["get"]["operationId"] == "getAdAccount"


def test_ad_accounts_share_the_clients_tag(offline_client: TestClient) -> None:
    """账户与客户是同一个业务领域，共用一个 tag。

    tag 的粒度对齐 docs/business/ 的领域划分 —— 拆成两个 tag 就意味着要拆成两篇
    业务文档，而账户脱离客户没有意义，规则本来就写在同一篇里。
    """
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/ad-accounts"]["post"]["tags"] == ["clients"]
    assert schema["paths"]["/api/clients"]["post"]["tags"] == ["clients"]


@pytest.mark.integration
async def test_create_then_read_back(live_api: AsyncClient) -> None:
    client_id = await _new_client(live_api, "测试客户-账户往返")

    created = await live_api.post("/api/ad-accounts", json=_payload(client_id))
    assert created.status_code == 201, created.text

    body = created.json()
    assert body["platform"] == "tiktok"
    assert body["timezone"] == "America/Anchorage"
    assert body["is_active"] is True

    fetched = await live_api.get(f"/api/ad-accounts/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


@pytest.mark.integration
async def test_unknown_client_is_422_not_404(live_api: AsyncClient) -> None:
    """`client_id` 指向不存在的客户 → 422。

    **不是 404**：URL 指的资源（`/ad-accounts` 这个集合）是存在的，不合法的是
    请求体。两者对客户端意味着不同的动作 —— 404 是「换个 URL」，422 是「改请求体」。
    """
    response = await live_api.post("/api/ad-accounts", json=_payload(999999))

    assert response.status_code == 422
    assert "客户不存在" in response.json()["detail"]


@pytest.mark.integration
async def test_duplicate_external_id_on_same_platform_is_a_conflict(
    live_api: AsyncClient,
) -> None:
    """同一平台上同一个账户 ID 只能有一行。

    这条唯一约束是幂等导入的依据：重复建一个同名账户，会让同一天的数据分裂到
    两行 —— 汇总时两边都对，加起来才是全部，而没人会想到去加。
    """
    client_id = await _new_client(live_api, "测试客户-账户重复")
    payload = _payload(client_id, external_id="acct-dup")
    assert (await live_api.post("/api/ad-accounts", json=payload)).status_code == 201

    duplicate = await live_api.post("/api/ad-accounts", json=payload)

    assert duplicate.status_code == 409
    assert "acct-dup" in duplicate.json()["detail"]


@pytest.mark.integration
async def test_same_external_id_on_another_platform_is_fine(live_api: AsyncClient) -> None:
    """唯一键是 (平台, 账户 ID) 的组合 —— 两个平台各有一个同号账户是正常的。"""
    client_id = await _new_client(live_api, "测试客户-跨平台同号")
    payload = _payload(client_id, external_id="acct-same")

    assert (await live_api.post("/api/ad-accounts", json=payload)).status_code == 201
    other = await live_api.post(
        "/api/ad-accounts",
        json=payload | {"platform": "meta"},
    )

    assert other.status_code == 201


@pytest.mark.integration
async def test_identity_fields_cannot_be_patched(live_api: AsyncClient) -> None:
    """`platform` 与 `external_id` 改不动 —— 请求体里根本没有这两个字段。

    钉住这个行为是因为它是静默的：多传的字段被 Pydantic 丢掉，客户端拿到 200
    会以为改成功了。改了才危险 —— 历史 daily_metrics 仍挂在原 account_id 上，
    那些数据会突然对不上平台。
    """
    client_id = await _new_client(live_api, "测试客户-改身份")
    created = (await live_api.post("/api/ad-accounts", json=_payload(client_id))).json()

    patched = await live_api.patch(
        f"/api/ad-accounts/{created['id']}",
        json={"platform": "meta", "external_id": "acct-hijack", "name": "改过的名字"},
    )

    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "改过的名字"
    assert body["platform"] == "tiktok"
    assert body["external_id"] == created["external_id"]


@pytest.mark.integration
async def test_list_can_filter_by_client(live_api: AsyncClient) -> None:
    """内部后台要按客户看账户，这是最常用的一个过滤。"""
    mine = await _new_client(live_api, "测试客户-筛选甲")
    other = await _new_client(live_api, "测试客户-筛选乙")
    await live_api.post("/api/ad-accounts", json=_payload(mine, external_id="acct-mine"))
    await live_api.post("/api/ad-accounts", json=_payload(other, external_id="acct-other"))

    response = await live_api.get("/api/ad-accounts", params={"client_id": mine})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "acct-mine"
