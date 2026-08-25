"""登录验证码：纯计算部分、状态部分，以及那条「顺序就是安全性」的门禁。

设计见 [登录验证码](../docs/design/2026-08-25-login-captcha.md)。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from adpilot.api.deps import get_redis
from adpilot.auth import captcha
from adpilot.config import Settings
from adpilot.services import login_guard


class FakeRedis:
    """够用的内存版 Redis。

    自己写而不是引 `fakeredis`：这里只用到五个命令，而多一个测试依赖就多一份
    要跟着升级的东西。`fail_on` 让用例能模拟「Redis 挂了」——那两个方向的取舍
    正是 `login_guard` 里最容易写反的地方。
    """

    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.store: dict[str, str] = {}
        self.fail_on = fail_on

    def _guard(self, command: str) -> None:
        if command in self.fail_on:
            raise ConnectionError(f"fake redis: {command} 不可用")

    async def get(self, key: str) -> str | None:
        self._guard("get")
        return self.store.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self._guard("setex")
        self.store[key] = value

    async def getdel(self, key: str) -> str | None:
        self._guard("getdel")
        return self.store.pop(key, None)

    async def delete(self, key: str) -> None:
        self._guard("delete")
        self.store.pop(key, None)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, str]] = []

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def expire(self, key: str, _ttl: int) -> None:
        self.ops.append(("expire", key))

    async def execute(self) -> None:
        self.redis._guard("pipeline")
        for op, key in self.ops:
            if op == "incr":
                self.redis.store[key] = str(int(self.redis.store.get(key, "0")) + 1)


def _r(fake: FakeRedis) -> Redis:
    """把 FakeRedis 伪装成 Redis 交给被测函数。

    cast 收在这一个函数里，而不是散在每个调用点 —— 散着写的话，哪天真的传错了
    类型也看不出来。
    """
    return cast("Redis", fake)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


# --- 纯计算部分（不需要 Redis）-----------------------------------------------


def test_answer_only_uses_the_unambiguous_alphabet() -> None:
    """出题只用剔过易混字符的表 —— 抄错三遍的代价落在合法用户身上。"""
    for _ in range(200):
        answer = captcha.new_answer()
        assert len(answer) == captcha.LENGTH
        assert set(answer) <= set(captcha.ALPHABET)


@pytest.mark.parametrize(
    ("expected", "supplied", "ok"),
    [
        ("AB34", "AB34", True),
        ("AB34", "ab34", True),  # 大小写不敏感
        ("AB34", " ab34 ", True),  # 两侧空白忽略
        ("AB34", "AB35", False),
        ("AB34", "", False),
        ("", "AB34", False),  # 没有期望值时一律不通过
        ("AB34", "AB3", False),
    ],
)
def test_answer_matching(expected: str, supplied: str, ok: bool) -> None:
    assert captcha.matches(expected, supplied) is ok


def test_render_rejects_characters_outside_the_alphabet() -> None:
    """SVG 里不做 XML 转义，所以这条约束必须由函数自己守住，而不是只写在注释里。"""
    with pytest.raises(ValueError, match="ALPHABET"):
        captcha.render_svg("<script>")


def test_rendered_svg_is_well_formed() -> None:
    import xml.etree.ElementTree as ET

    answer = captcha.new_answer()
    svg = captcha.render_svg(answer)
    root = ET.fromstring(svg)  # 自己刚生成的字符串，不是外部输入

    assert root.tag.endswith("svg")
    texts = [node.text for node in root.iter() if node.tag.endswith("text")]
    assert "".join(t or "" for t in texts) == answer


# --- 状态部分 ---------------------------------------------------------------


async def test_captcha_appears_only_after_two_failures(redis: FakeRedis) -> None:
    """一次是手滑，两次是没记住，三次开始像在试。"""
    assert await login_guard.captcha_required(_r(redis), username="admin") is False

    await login_guard.record_failure(_r(redis), username="admin")
    assert await login_guard.captcha_required(_r(redis), username="admin") is False

    await login_guard.record_failure(_r(redis), username="admin")
    assert await login_guard.captcha_required(_r(redis), username="admin") is True


async def test_success_clears_the_counter(redis: FakeRedis) -> None:
    for _ in range(3):
        await login_guard.record_failure(_r(redis), username="admin")
    assert await login_guard.captcha_required(_r(redis), username="admin") is True

    await login_guard.clear_failures(_r(redis), username="admin")
    assert await login_guard.captcha_required(_r(redis), username="admin") is False


async def test_counters_are_per_username(redis: FakeRedis) -> None:
    for _ in range(3):
        await login_guard.record_failure(_r(redis), username="admin")
    assert await login_guard.captcha_required(_r(redis), username="someone-else") is False


async def test_a_captcha_is_consumed_even_when_the_answer_is_right(redis: FakeRedis) -> None:
    """🔴 用一次即删。留着的话一张答对过的码能驱动任意多次密码尝试。"""
    captcha_id, _ = await login_guard.issue_captcha(_r(redis))
    answer = redis.store[f"login:captcha:{captcha_id}"]

    assert (
        await login_guard.consume_captcha(_r(redis), captcha_id=captcha_id, answer=answer) is True
    )
    # 同一张再用一次就不认了。
    assert (
        await login_guard.consume_captcha(_r(redis), captcha_id=captcha_id, answer=answer) is False
    )


async def test_a_captcha_is_consumed_even_when_the_answer_is_wrong(redis: FakeRedis) -> None:
    """答错也要销毁 —— 否则同一张图可以被反复拿去配不同的密码。"""
    captcha_id, _ = await login_guard.issue_captcha(_r(redis))

    assert (
        await login_guard.consume_captcha(_r(redis), captcha_id=captcha_id, answer="ZZZZ") is False
    )
    assert f"login:captcha:{captcha_id}" not in redis.store


async def test_unknown_captcha_id_is_rejected(redis: FakeRedis) -> None:
    assert await login_guard.consume_captcha(_r(redis), captcha_id="nope", answer="AB34") is False


async def test_redis_down_does_not_lock_people_out() -> None:
    """🔴 连不上 Redis 时选「不要验证码」——否则一次抖动就把要去修它的人关在外面。"""
    broken = FakeRedis(fail_on=frozenset({"get"}))
    assert await login_guard.captcha_required(_r(broken), username="admin") is False


async def test_redis_down_does_not_let_a_required_captcha_pass() -> None:
    """反方向：已经确定要验证码了，验不了就不能放行。"""
    broken = FakeRedis(fail_on=frozenset({"getdel"}))
    assert await login_guard.consume_captcha(_r(broken), captcha_id="x", answer="AB34") is False


# --- 接口行为 ---------------------------------------------------------------


@pytest.fixture
def app_with_redis(offline_app: FastAPI, redis: FakeRedis) -> Any:
    offline_app.dependency_overrides[get_redis] = lambda: _r(redis)
    yield offline_app
    offline_app.dependency_overrides.pop(get_redis, None)


def test_captcha_endpoint_says_no_until_the_threshold(app_with_redis: FastAPI) -> None:
    with TestClient(app_with_redis) as client:
        body = client.get("/api/auth/captcha", params={"username": "admin"}).json()
    assert body["required"] is False
    # 不需要时不生成 —— 否则每次打开登录页都往 Redis 塞一张没人会用的答案。
    assert body["captcha_id"] is None
    assert body["image"] is None


def test_captcha_endpoint_serves_an_image_after_the_threshold(
    app_with_redis: FastAPI, redis: FakeRedis
) -> None:
    redis.store["login:fail:admin"] = "2"

    with TestClient(app_with_redis) as client:
        body = client.get("/api/auth/captcha", params={"username": "admin"}).json()

    assert body["required"] is True
    assert body["captcha_id"]
    assert body["image"].startswith("data:image/svg+xml;base64,")


def test_wrong_password_is_counted(app_with_redis: FastAPI, redis: FakeRedis) -> None:
    with TestClient(app_with_redis) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert redis.store["login:fail:admin"] == "1"


def test_successful_login_clears_the_counter(
    app_with_redis: FastAPI, redis: FakeRedis, operator_password: str, offline_settings: Settings
) -> None:
    # 🔴 计数按**请求里的用户名**记，而夹具里那个账号不叫 admin —— 预置错 key 的话
    # 这条会以「清零没生效」的样子失败，而实际什么毛病都没有。
    key = f"login:fail:{offline_settings.operator_username}"
    redis.store[key] = "1"

    with TestClient(app_with_redis) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": offline_settings.operator_username, "password": operator_password},
        )

    assert response.status_code == 200
    assert key not in redis.store


def test_captcha_is_checked_before_the_password_hash(
    app_with_redis: FastAPI,
    redis: FakeRedis,
    operator_password: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴🔴 **这条是这个功能的全部意义。**

    验证码若排在 argon2 之后，攻击者拿一个空的答案照样能驱动服务端做无限次哈希
    —— 爆破速率一点没降，而**没有任何别的测试会因此变红**。所以只能直接盯：
    在「需要验证码但没带」的情况下，密码校验函数一次都不许被调用。
    """
    calls: list[str] = []

    def spy(**kwargs: Any) -> bool:
        calls.append(kwargs["username"])
        return True

    monkeypatch.setattr("adpilot.api.auth.verify_operator", spy)
    redis.store["login:fail:admin"] = "2"

    with TestClient(app_with_redis) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": operator_password},
        )

    assert response.status_code == 401
    assert calls == [], "验证码没过就不该走到 argon2 —— 顺序被写反了"


def test_a_failed_captcha_still_counts(
    app_with_redis: FastAPI, redis: FakeRedis, operator_password: str
) -> None:
    """否则「验证码随便填」就是一条不涨计数的免费重试通道。"""
    redis.store["login:fail:admin"] = "2"

    with TestClient(app_with_redis) as client:
        client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": operator_password,
                "captcha_id": "nope",
                "captcha_answer": "ZZZZ",
            },
        )

    assert redis.store["login:fail:admin"] == "3"
