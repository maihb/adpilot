"""登录与续期。

不连库 —— 运营账号在环境变量里，这条链路上没有数据库。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from adpilot.auth.token import Scope, verify
from adpilot.config import Settings
from adpilot.main import create_app


@pytest.fixture
def unconfigured_client(offline_settings: Settings) -> Iterator[TestClient]:
    """一台没填 `AUTH_SECRET` 的实例。"""
    settings = offline_settings.model_copy(update={"auth_secret": SecretStr("")})
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, username: str, password: str) -> Response:
    # 显式标注：starlette 的 TestClient 存根把返回值标成了 Any。
    response: Response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    return response


def test_login_returns_a_usable_token(
    anonymous_client: TestClient,
    offline_settings: Settings,
    operator_password: str,
) -> None:
    response = _login(anonymous_client, offline_settings.operator_username, operator_password)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"].startswith("v1.")
    assert body["expires_at"], "没带到期时间，前端就只能等 401 再补救"

    # 换出来的确实是一张验得过的运营票。**不去打一个受保护接口来验** ——
    # 这套夹具哪儿都连不上，那样只会撞在数据库上，测出来的东西也不是这条链路。
    payload = verify(offline_settings.auth_secret, body["token"], scope=Scope.OPERATOR)
    assert payload.sub == offline_settings.operator_username


def test_a_wrong_password_and_a_wrong_username_look_identical(
    anonymous_client: TestClient,
    offline_settings: Settings,
    operator_password: str,
) -> None:
    """两种失败必须**一模一样**：分得开就等于确认「这个用户名是存在的」。"""
    wrong_password = _login(anonymous_client, offline_settings.operator_username, "错的密码")
    wrong_username = _login(anonymous_client, "谁啊", operator_password)

    assert wrong_password.status_code == 401
    assert wrong_username.status_code == 401
    assert wrong_password.json() == wrong_username.json()


def test_login_challenges_with_bearer(
    anonymous_client: TestClient,
    offline_settings: Settings,
) -> None:
    response = _login(anonymous_client, offline_settings.operator_username, "错的密码")

    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_login_input_is_validated_by_pydantic(anonymous_client: TestClient) -> None:
    """入参校验不写手工分支 —— 这条守着它确实生效。

    密码那条上限盯的是「拿一兆的字符串去打 argon2」：那个哈希故意很慢，而登录
    接口不需要认证就能打。
    """
    assert anonymous_client.post("/api/auth/login", json={}).status_code == 422
    assert (
        anonymous_client.post(
            "/api/auth/login",
            json={"username": "x", "password": "y" * 1025},
        ).status_code
        == 422
    )


def test_refresh_hands_out_a_fresh_token(offline_client: TestClient) -> None:
    response = offline_client.post("/api/auth/refresh")

    assert response.status_code == 200, response.text
    assert response.json()["token"].startswith("v1.")


def test_refresh_needs_a_valid_token(anonymous_client: TestClient) -> None:
    """过期了就只能重新登录 —— 能拿过期 token 续期的话，有效期等于没有。"""
    assert anonymous_client.post("/api/auth/refresh").status_code == 401


def test_an_unconfigured_instance_says_so_instead_of_rejecting_the_password(
    unconfigured_client: TestClient,
    offline_settings: Settings,
    operator_password: str,
) -> None:
    """没配 `AUTH_SECRET` 要回 **503**，不是 401。

    这是部署方的问题不是调用方的问题 —— 回 401 会让人对着一个永远进不去的登录框
    试半天密码。响应体里说清缺了哪个环境变量，那句话不含任何敏感内容
    （`.env.example` 里本来就写着）。
    """
    response = _login(unconfigured_client, offline_settings.operator_username, operator_password)

    assert response.status_code == 503
    assert "AUTH_SECRET" in response.json()["detail"]
