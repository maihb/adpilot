"""邀请码：生成、列出、作废、兑换。

兑换那条链是**唯一不需要认证就能打的写路径**（它会改 `use_count`），所以失败
路径比成功路径更值得写：码无效、过期、被作废、客户停止合作，四种都必须长得
一模一样。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.services import client as client_service
from adpilot.services import invite as invite_service
from adpilot.services.exceptions import NotFoundError


def test_openapi_declares_stable_operation_ids(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()
    invites = schema["paths"]["/api/clients/{client_id}/invites"]

    assert invites["post"]["operationId"] == "createInvite"
    assert invites["get"]["operationId"] == "listInvites"
    assert schema["paths"]["/api/auth/redeem"]["post"]["operationId"] == "redeemInvite"


def test_the_listing_schema_has_no_field_for_the_code(offline_client: TestClient) -> None:
    """列表出参里**不能有 `code`**。

    库里存的是哈希，本来就还原不回来 —— 这条守的是「哪天有人为了方便，把明文
    也存一份」。schema 里没有这个字段，那种改动就会先在这里红。
    """
    schema = offline_client.get("/openapi.json").json()

    assert "code" not in schema["components"]["schemas"]["InviteResponse"]["properties"]
    assert "code" in schema["components"]["schemas"]["InviteCreatedResponse"]["properties"]


def test_ttl_is_bounded(offline_client: TestClient) -> None:
    """不给「永不过期」这个选项：那等于给这个客户的数据配了一把永久钥匙。"""
    for ttl in (0, 366):
        response = offline_client.post("/api/clients/1/invites", json={"ttl_days": ttl})
        assert response.status_code == 422, f"ttl_days={ttl} 没被拦住"


async def _client_and_invite(api: AsyncClient, name: str) -> tuple[int, str]:
    """建一个客户并给它生成一个邀请码，返回 (client_id, 明文码)。"""
    client_id = (await api.post("/api/clients", json={"name": name})).json()["id"]
    created = await api.post(f"/api/clients/{client_id}/invites", json={})
    assert created.status_code == 201, created.text
    return client_id, created.json()["code"]


@pytest.mark.integration
async def test_a_fresh_code_buys_a_client_token(live_api: AsyncClient) -> None:
    """扫码进来这一下：码换 token，顺带把客户名带回去。

    名字带回去是为了让小程序第一屏就能显示「XX 的投放看板」，不必再问一次
    「我是谁」—— 那是这条链路上唯一会被用户感知到的等待。
    """
    _, code = await _client_and_invite(live_api, "测试客户-邀请码-兑换")

    redeemed = await live_api.post("/api/auth/redeem", json={"code": code})

    assert redeemed.status_code == 200, redeemed.text
    body = redeemed.json()
    assert body["token"].startswith("v1.")
    assert body["client_name"] == "测试客户-邀请码-兑换"

    # 换来的客户端 token 真的能用（这里挑续期接口，它是目前唯一的客户端入口）
    refreshed = await live_api.post(
        "/api/auth/client/refresh",
        headers={"Authorization": f"Bearer {body['token']}"},
    )
    assert refreshed.status_code == 200, refreshed.text


@pytest.mark.integration
async def test_the_same_code_works_more_than_once(live_api: AsyncClient) -> None:
    """**刻意不是一次性的。**

    一个客户往往老板和运营两个人都要看，还会换手机、清缓存 —— 每次都要运营重新
    生成一个，那是把成本转嫁给唯一不该被打扰的人。
    """
    client_id, code = await _client_and_invite(live_api, "测试客户-邀请码-多次")

    for _ in range(3):
        assert (await live_api.post("/api/auth/redeem", json={"code": code})).status_code == 200

    listed = (await live_api.get(f"/api/clients/{client_id}/invites")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["use_count"] == 3
    assert listed["items"][0]["last_used_at"], "用过了却没记下来，运营就查不到码发出去没有"
    assert "code" not in listed["items"][0], "明文码不该出现在列表里"


@pytest.mark.integration
async def test_a_revoked_code_stops_working(live_api: AsyncClient) -> None:
    client_id, code = await _client_and_invite(live_api, "测试客户-邀请码-作废")
    invite_id = (await live_api.get(f"/api/clients/{client_id}/invites")).json()["items"][0]["id"]

    revoked = await live_api.post(f"/api/clients/{client_id}/invites/{invite_id}/revoke")

    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"]
    assert (await live_api.post("/api/auth/redeem", json={"code": code})).status_code == 404


@pytest.mark.integration
async def test_revoking_twice_keeps_the_first_timestamp(live_api: AsyncClient) -> None:
    """「什么时候断的」不该被第二次点击覆盖掉。"""
    client_id, _ = await _client_and_invite(live_api, "测试客户-邀请码-重复作废")
    invite_id = (await live_api.get(f"/api/clients/{client_id}/invites")).json()["items"][0]["id"]

    first = await live_api.post(f"/api/clients/{client_id}/invites/{invite_id}/revoke")
    second = await live_api.post(f"/api/clients/{client_id}/invites/{invite_id}/revoke")

    assert second.status_code == 200
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


@pytest.mark.integration
async def test_another_clients_invite_cannot_be_revoked(live_api: AsyncClient) -> None:
    """路径里的 `client_id` 必须参与查询，否则它就只是个装饰。

    拿 B 客户的路径去作废 A 客户的码，要 404 而不是「作废成功」。
    """
    a_id, _ = await _client_and_invite(live_api, "测试客户-邀请码-归属 A")
    b_id, _ = await _client_and_invite(live_api, "测试客户-邀请码-归属 B")
    a_invite_id = (await live_api.get(f"/api/clients/{a_id}/invites")).json()["items"][0]["id"]

    response = await live_api.post(f"/api/clients/{b_id}/invites/{a_invite_id}/revoke")

    assert response.status_code == 404


@pytest.mark.integration
async def test_an_expired_code_stops_working(live_session: AsyncSession) -> None:
    """过期的码换不出 token。

    直接调服务层：接口上的 `ttl_days` 最小是 1 天，从接口造不出一个已经过期的码
    —— 而「等一天再断言」不是测试。
    """
    client = await client_service.create(live_session, name="测试客户-邀请码-过期")
    _, code = await invite_service.create(
        live_session,
        client_id=client.id,
        ttl=timedelta(days=1),
    )

    later = datetime.now(UTC) + timedelta(days=2)
    with pytest.raises(NotFoundError, match="无效"):
        await invite_service.redeem(live_session, code, now=later)


@pytest.mark.integration
async def test_a_stopped_client_cannot_be_redeemed_into(live_api: AsyncClient) -> None:
    """停止合作的客户，码跟着失效。

    否则「不再服务这个客户」这件事要靠人记得去逐个作废他手上的码。
    """
    client_id, code = await _client_and_invite(live_api, "测试客户-邀请码-已停用")

    await live_api.patch(f"/api/clients/{client_id}", json={"is_active": False})

    assert (await live_api.post("/api/auth/redeem", json={"code": code})).status_code == 404


@pytest.mark.integration
async def test_stopping_a_client_cuts_off_tokens_already_handed_out(live_api: AsyncClient) -> None:
    """**这是自包含 token 唯一的「踢人」手段。**

    token 撤销不了，但客户端每次请求都会确认「这个客户还在合作吗」（`api/deps.py`
    的 `require_client_scope`）。少了那一次查询，停止合作之后对方仍能看 7 天。
    """
    client_id, code = await _client_and_invite(live_api, "测试客户-邀请码-踢人")
    token = (await live_api.post("/api/auth/redeem", json={"code": code})).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert (await live_api.post("/api/auth/client/refresh", headers=headers)).status_code == 200

    await live_api.patch(f"/api/clients/{client_id}", json={"is_active": False})

    assert (await live_api.post("/api/auth/client/refresh", headers=headers)).status_code == 401


@pytest.mark.integration
async def test_a_made_up_code_is_a_404(live_api: AsyncClient) -> None:
    response = await live_api.post("/api/auth/redeem", json={"code": "compl3tely-made-up"})

    assert response.status_code == 404
    assert "无效" in response.json()["detail"]


@pytest.mark.integration
async def test_invites_for_a_missing_client_are_404(live_api: AsyncClient) -> None:
    assert (await live_api.get("/api/clients/999999/invites")).status_code == 404
    assert (await live_api.post("/api/clients/999999/invites", json={})).status_code == 404
