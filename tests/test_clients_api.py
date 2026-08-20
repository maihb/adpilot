"""客户接口的测试。

两档：**不连库那几条验契约**（入参校验、OpenAPI 形状 —— 它们在 handler 执行前
就已成败已定），**连库那几条验往返和失败路径**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient


def test_openapi_declares_stable_operation_ids(offline_client: TestClient) -> None:
    """两个前端按 `operationId` 生成方法名，漏了就会拿到一坨自动拼的名字。

    自动拼的那种（`create_client_api_clients_post`）还有个更糟的性质：**它跟着
    路径走**，改一次路由前缀，所有前端调用点一起断。
    """
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/clients"]["post"]["operationId"] == "createClient"
    assert schema["paths"]["/api/clients"]["get"]["operationId"] == "listClients"
    assert schema["paths"]["/api/clients/{client_id}"]["get"]["operationId"] == "getClient"
    assert schema["paths"]["/api/clients/{client_id}"]["patch"]["operationId"] == "updateClient"


def test_error_responses_are_declared(offline_client: TestClient) -> None:
    """4xx 也要有 schema。

    不声明的话生成出来的客户端会以为这个接口只可能成功，而错误分支恰恰是最需要
    类型的地方。
    """
    schema = offline_client.get("/openapi.json").json()
    post = schema["paths"]["/api/clients"]["post"]["responses"]

    assert "409" in post
    assert post["409"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")


def test_client_without_a_name_is_rejected(offline_client: TestClient) -> None:
    """入参校验交给 Pydantic，不写手工分支 —— 这条守着它确实生效。"""
    assert offline_client.post("/api/clients", json={}).status_code == 422
    assert offline_client.post("/api/clients", json={"name": ""}).status_code == 422


def test_page_size_over_the_cap_is_rejected(offline_client: TestClient) -> None:
    """超上限要 422，**不能悄悄截断成 100**。

    截断的话客户端要了 500 条、拿到 100 条却看不出被截过，会把「后面没有了」
    当成数据到头了。
    """
    assert offline_client.get("/api/clients", params={"page_size": 101}).status_code == 422
    assert offline_client.get("/api/clients", params={"page": 0}).status_code == 422


def test_page_size_cap_is_visible_in_the_schema(offline_client: TestClient) -> None:
    """上限要出现在 OpenAPI 里，前端才能在发请求之前就知道它。

    只在服务端拦的话，前端得先撞一次 422 才知道边界在哪 —— 而这个值将来一旦调整，
    没人会想起去改前端里那个硬编码的 100。

    （合法边界值 100 能不能过，得连库才验得了，那条在集成测试里。）
    """
    schema = offline_client.get("/openapi.json").json()
    params = schema["paths"]["/api/clients"]["get"]["parameters"]
    page_size = next(p for p in params if p["name"] == "page_size")

    assert page_size["schema"]["maximum"] == 100
    assert page_size["schema"]["default"] == 20


@pytest.mark.integration
async def test_create_then_read_back(live_api: AsyncClient) -> None:
    """建出来的客户要能原样查回来，且带上服务端生成的字段。"""
    created = await live_api.post(
        "/api/clients",
        json={"name": "测试客户-往返", "note": "季度结算"},
    )
    assert created.status_code == 201, created.text

    body = created.json()
    assert body["name"] == "测试客户-往返"
    assert body["is_active"] is True
    assert body["created_at"], "server_default 生成的时间戳没有取回来"

    fetched = await live_api.get(f"/api/clients/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


@pytest.mark.integration
async def test_duplicate_name_is_a_conflict(live_api: AsyncClient) -> None:
    """重名要 409 而不是建出第二行。

    客户名唯一不是洁癖：文件导入按名字幂等找回客户（CSV 里通常只有客户名没有
    ID），有两行同名的话，同一个客户的数据会分裂到两边。
    """
    payload = {"name": "测试客户-重名"}
    assert (await live_api.post("/api/clients", json=payload)).status_code == 201

    duplicate = await live_api.post("/api/clients", json=payload)
    assert duplicate.status_code == 409
    assert "已存在" in duplicate.json()["detail"]


@pytest.mark.integration
async def test_missing_client_is_404(live_api: AsyncClient) -> None:
    response = await live_api.get("/api/clients/999999")

    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


@pytest.mark.integration
async def test_patch_leaves_untouched_fields_alone(live_api: AsyncClient) -> None:
    """PATCH 只动请求体里出现的字段。

    这条盯的是 `exclude_unset=True`：少了它，没传的字段会被当成 null 一起写进去
    —— 改个 `is_active` 顺手把备注清空，而且没有任何报错。
    """
    created = (
        await live_api.post(
            "/api/clients",
            json={"name": "测试客户-局部改", "note": "原备注"},
        )
    ).json()

    patched = await live_api.patch(
        f"/api/clients/{created['id']}",
        json={"is_active": False},
    )

    assert patched.status_code == 200
    body = patched.json()
    assert body["is_active"] is False
    assert body["note"] == "原备注"


@pytest.mark.integration
async def test_note_can_be_cleared_explicitly(live_api: AsyncClient) -> None:
    """显式传 null 要能把备注清空 —— 这是「没传」与「传了 null」的分界。"""
    created = (
        await live_api.post(
            "/api/clients",
            json={"name": "测试客户-清备注", "note": "待删"},
        )
    ).json()

    cleared = await live_api.patch(f"/api/clients/{created['id']}", json={"note": None})

    assert cleared.status_code == 200
    assert cleared.json()["note"] is None


@pytest.mark.integration
async def test_list_returns_filtered_total_not_page_size(live_api: AsyncClient) -> None:
    """`total` 是过滤后的总数，不是本页条数 —— 前端要靠它算总页数。"""
    for index in range(3):
        await live_api.post("/api/clients", json={"name": f"测试客户-分页-{index}"})

    response = await live_api.get("/api/clients", params={"page_size": 2})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 3

    # 上限本身是合法值：只有**超出**才 422。单元测试那条只能验到 schema 里的
    # maximum，验不到真跑一次会不会被拦，两条合起来才盖住这个边界。
    capped = await live_api.get("/api/clients", params={"page_size": 100})
    assert capped.status_code == 200


@pytest.mark.integration
async def test_list_can_filter_by_active_flag(live_api: AsyncClient) -> None:
    """停止合作的客户默认还在列表里，要能单独筛掉。"""
    created = (await live_api.post("/api/clients", json={"name": "测试客户-已停用"})).json()
    await live_api.patch(f"/api/clients/{created['id']}", json={"is_active": False})

    active = await live_api.get("/api/clients", params={"is_active": True})
    inactive = await live_api.get("/api/clients", params={"is_active": False})

    assert created["id"] not in [item["id"] for item in active.json()["items"]]
    assert created["id"] in [item["id"] for item in inactive.json()["items"]]
