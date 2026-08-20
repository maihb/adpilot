"""「这个接口忘了要认证」的门禁。

**为什么要有这么一条测试。** 认证是在 `main.py` 里按 router 统一挂的，而挂漏一个
不会有任何报错 —— 它只会变成一条谁都能调的接口，而且看起来一切正常。这类漏没有
症状，所以只能让机器每次都数一遍。

判据取的是 openapi.json 里的 `security` 字段，理由和 `test_business_docs.py` 拿
tag 当锚点一样：**挑一个「忘了就注册不出来」的东西去验**。security 是 FastAPI 从
依赖树里收集出来的，写不了假 —— 挂了依赖它就在，没挂就没有。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from adpilot.auth.token import Scope, issue
from adpilot.config import Settings

# 🔴 **免认证的接口清单。加一条公开接口就必须来改这里。**
#
# 写成一份显式的表，是为了让「又多了一个不要认证的入口」变成一次 review 时看得见
# 的改动，而不是某个 router 忘了挂依赖的副产品。下面那条断言**两个方向都查**：
# 漏挂认证会红，清单里留着一条其实已经要认证的也会红（说明清单过期了）。
PUBLIC_OPERATIONS = frozenset(
    {
        # 探针的调用方是 Docker 和负载均衡，它们没地方放 token；而一个需要凭据
        # 才能回答「你还活着吗」的探针，会把配置错误表现成服务挂掉。
        "get /api/health/live",
        "get /api/health/ready",
        # 换 token 的两个入口自己不能要 token。
        "post /api/auth/login",
        "post /api/auth/redeem",
    }
)

#: 要**客户身份**的接口。`/api/portal/` 下的全部路由自动算在内（按前缀判），
#: 这份清单是那个前缀之外的例外 —— 目前只有客户端续期，它属于认证链路，所以
#: 挂在 `/api/auth/` 下面。
PORTAL_PREFIX = "/api/portal/"
CLIENT_OPERATIONS = frozenset({"post /api/auth/client/refresh"})


def _wants_client_scope(name: str) -> bool:
    return name in CLIENT_OPERATIONS or PORTAL_PREFIX in name


def _operations(client: TestClient) -> dict[str, dict[str, Any]]:
    schema = client.get("/openapi.json").json()
    return {
        f"{method} {path}": operation
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
    }


def test_every_api_operation_requires_authentication(offline_client: TestClient) -> None:
    """除了清单里那三条，每个接口都必须带 security。"""
    public = {
        name
        for name, operation in _operations(offline_client).items()
        if not operation.get("security")
    }

    assert public == PUBLIC_OPERATIONS, (
        "免认证的接口和清单对不上。多出来的那些多半是 main.py 里漏挂了 "
        "require_operator；少掉的说明清单过期了，删掉对应那行。"
    )


def test_internal_operations_ask_for_the_operator_scheme(offline_client: TestClient) -> None:
    """内部接口要的必须是**运营**身份。

    两个 scheme 名字不同，正是为了让这件事验得出来 —— 合成一个的话，「内部接口
    被挂成了客户端身份」这种错就再也看不见了。
    """
    schemes = {
        name: [key for requirement in operation["security"] for key in requirement]
        for name, operation in _operations(offline_client).items()
        if name not in PUBLIC_OPERATIONS and not _wants_client_scope(name)
    }

    assert schemes, "一条内部接口都没找到，这条测试自己失效了"
    for name, required in schemes.items():
        assert required == ["OperatorBearer"], f"{name} 要的不是运营身份：{required}"


def test_client_facing_operations_ask_for_the_client_scheme(offline_client: TestClient) -> None:
    """**作用域门禁的第一层。**

    客户端路由必须要 `ClientBearer` —— 挂成运营身份的话，那个接口就成了「任何
    运营 token 都能调、而且没有 client_id 可以过滤」的形状。

    这条同时管住 `/api/portal/` 下的每一个新路由：加了却忘了声明作用域依赖，
    这里当场红。加一条客户端接口而不放在那个前缀下，也要来这里登记一行。
    """
    schemes = {
        name: [key for requirement in operation["security"] for key in requirement]
        for name, operation in _operations(offline_client).items()
        if _wants_client_scope(name)
    }

    assert schemes, "一条客户端接口都没找到，这条测试自己失效了"
    for name, required in schemes.items():
        assert required == ["ClientBearer"], f"{name} 要的不是客户身份：{required}"


def test_anonymous_requests_are_rejected_with_a_challenge(anonymous_client: TestClient) -> None:
    """没带 token 要 401（**不是 403**）并带上 WWW-Authenticate。

    403 的语义是「你是谁我知道，但不让你进」，客户端据此不会去走重新登录的流程
    —— 而 `HTTPBearer` 的默认行为恰恰是返回 403，所以这条盯的是 `auto_error=False`
    那个开关有没有被改回去。
    """
    response = anonymous_client.get("/api/clients")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_health_probes_stay_open(anonymous_client: TestClient) -> None:
    """探针不能被认证挡住 —— 挡住的话，凭据配错会表现成「服务挂了」。"""
    assert anonymous_client.get("/api/health/live").status_code == 200


def test_a_client_token_cannot_call_internal_endpoints(
    anonymous_client: TestClient,
    offline_settings: Settings,
) -> None:
    """**跨作用域越权。**

    客户端 token 的签名是完全合法的，只是拿错了门。漏掉 scope 比对的话，任何一个
    客户都能建客户、导数据、翻出全部客户的花费 —— 而且不会有任何报错。
    """
    token, _ = issue(offline_settings.auth_secret, scope=Scope.CLIENT, sub="1")

    response = anonymous_client.get("/api/clients", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_garbage_tokens_are_rejected_not_crashed_on(anonymous_client: TestClient) -> None:
    """认证入口是不需要凭据就能打的，所以它不能被任意字符串打成 500。"""
    # 全是 ASCII：httpx 不让非 ASCII 进请求头，那是客户端侧的限制，跟服务端无关。
    for value in ["Bearer", "Bearer ", "Bearer nonsense", "Basic dXNlcjpwYXNz", "v1.a.b"]:
        response = anonymous_client.get("/api/clients", headers={"Authorization": value})
        assert response.status_code == 401, f"{value!r} 换来了 {response.status_code}"


def test_operator_tokens_signed_with_another_secret_are_rejected(
    anonymous_client: TestClient,
) -> None:
    """换 `AUTH_SECRET` 是唯一的「全体失效」手段，它必须在接口这一层也真的有效。"""
    other = SecretStr("test-not-a-secret-fedcba9876543210")
    token, _ = issue(other, scope=Scope.OPERATOR, sub="test-operator")

    response = anonymous_client.get("/api/clients", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
