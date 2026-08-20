"""HMAC token 的签发与校验。

这里验的**不是「能签能验」**（那种一跑就知道），是四件错了不会报错、只会悄悄
放行的事，逐条对着 `auth/token.py` 的模块 docstring：签名比对、验签与解析的顺序、
验的是不是原始字节、以及 scope 有没有真的比对。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from adpilot.auth import token as token_module
from adpilot.auth.token import (
    CLIENT_TOKEN_MAX_AGE,
    CLIENT_TOKEN_TTL,
    OPERATOR_TOKEN_TTL,
    AuthNotConfiguredError,
    InvalidTokenError,
    Scope,
    TokenPayload,
    issue,
    renew,
    verify,
)

# 一串刻意难看的假值，不是任何地方的真实密钥。
SECRET = SecretStr("test-not-a-secret-0123456789abcdef")
OTHER_SECRET = SecretStr("test-not-a-secret-fedcba9876543210")

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def test_round_trip_carries_scope_and_subject() -> None:
    token, expires_at = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)

    payload = verify(SECRET, token, scope=Scope.CLIENT, now=NOW)

    assert payload.scope is Scope.CLIENT
    assert payload.client_id == 42
    assert payload.iat == NOW
    assert expires_at == NOW + CLIENT_TOKEN_TTL


def test_operator_tokens_are_much_shorter_lived() -> None:
    """运营 token 的权限大得多 —— 它能导数据、能改客户。"""
    _, expires_at = issue(SECRET, scope=Scope.OPERATOR, sub="admin", now=NOW)

    assert expires_at == NOW + OPERATOR_TOKEN_TTL


def test_tampering_with_the_payload_breaks_verification() -> None:
    """改一个字节就必须验不过 —— 否则 payload 里的 `client_id` 就成了许愿池。"""
    token, _ = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)
    version, payload_part, signature_part = token.split(".")

    # 换掉 payload 里的一个字符，签名原样留着
    forged_payload = ("A" if payload_part[5] != "A" else "B").join(
        [payload_part[:5], payload_part[6:]]
    )

    with pytest.raises(InvalidTokenError):
        verify(SECRET, f"{version}.{forged_payload}.{signature_part}", scope=Scope.CLIENT, now=NOW)


def test_tampering_with_the_signature_breaks_verification() -> None:
    token, _ = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)
    version, payload_part, signature_part = token.split(".")
    forged = ("A" if signature_part[0] != "A" else "B") + signature_part[1:]

    with pytest.raises(InvalidTokenError):
        verify(SECRET, f"{version}.{payload_part}.{forged}", scope=Scope.CLIENT, now=NOW)


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    """换 `AUTH_SECRET` 是这个方案里唯一的「全体失效」手段，它必须真的有效。"""
    token, _ = issue(OTHER_SECRET, scope=Scope.CLIENT, sub="42", now=NOW)

    with pytest.raises(InvalidTokenError):
        verify(SECRET, token, scope=Scope.CLIENT, now=NOW)


def test_a_client_token_cannot_open_the_operator_door() -> None:
    """**最容易漏的一条。**

    只验签名的话，一个完全合法的客户端 token 就能调运营接口 —— 签名是真的，
    只是拿错了门。而这种漏不会有任何报错。
    """
    token, _ = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)

    with pytest.raises(InvalidTokenError):
        verify(SECRET, token, scope=Scope.OPERATOR, now=NOW)


def test_an_expired_token_is_rejected() -> None:
    token, _ = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)

    with pytest.raises(InvalidTokenError):
        verify(SECRET, token, scope=Scope.CLIENT, now=NOW + CLIENT_TOKEN_TTL + timedelta(seconds=1))


def test_garbage_never_raises_anything_but_invalid_token() -> None:
    """任何形状的垃圾都只能是一种失败。

    漏出别的异常类型（`ValueError`、`binascii.Error`）意味着接口层会把它当成
    500 —— 而一个能被任意字符串打成 500 的认证入口，本身就是可用性问题。
    """
    for garbage in ["", "x", "v1.x", "v2.a.b", "v1..", "v1.@@@.###", "a.b.c.d"]:
        with pytest.raises(InvalidTokenError):
            verify(SECRET, garbage, scope=Scope.CLIENT, now=NOW)


def test_renewal_slides_the_expiry_but_keeps_the_first_issued_at() -> None:
    """滑动过期：常看的人永远不用重扫。

    `iat` 必须原样保留 —— 它是绝对上限的计算依据，重置一次就等于把上限往后挪了
    一次，那条线也就形同虚设了。
    """
    token, _ = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)
    later = NOW + timedelta(days=5)

    renewed, expires_at = renew(SECRET, token, scope=Scope.CLIENT, now=later)
    payload = verify(SECRET, renewed, scope=Scope.CLIENT, now=later)

    assert payload.iat == NOW, "续期重置了 iat，绝对上限就永远到不了"
    assert expires_at == later + CLIENT_TOKEN_TTL


def test_renewal_is_capped_by_the_absolute_limit() -> None:
    """续到头的那一次，新 `exp` 要在上限处截断，而不是又往后 7 天。

    这里是**链式**续期（每 6 天回来看一次），不是拿最初那个 token 隔 88 天再续
    —— 后者根本续不动，它自己早过期了。滑动过期的真实形状就是这条链，顺带也验
    了「续了十几次，`iat` 仍是第一次签发的时间」。
    """
    token, expires_at = issue(SECRET, scope=Scope.CLIENT, sub="42", now=NOW)
    clock = NOW

    for _ in range(14):  # 14 × 6 天 = 84 天，每一步都还在上限之内
        clock += timedelta(days=6)
        token, expires_at = renew(SECRET, token, scope=Scope.CLIENT, now=clock)

    # 最后这次本该续到第 91 天，被上限拦在第 90 天
    assert clock == NOW + timedelta(days=84)
    assert expires_at == NOW + CLIENT_TOKEN_MAX_AGE
    assert verify(SECRET, token, scope=Scope.CLIENT, now=clock).iat == NOW


def test_renewal_past_the_absolute_limit_is_refused() -> None:
    """到了 90 天就得重新扫码 —— 这是唯一的强制重新认证时刻。

    手工签一个「iat 是 100 天前、exp 还没到」的 token 来问：正常路径签不出它
    （签发侧会截断），但**拒绝的责任在验证侧** —— 这条正是走那个兜底。
    """
    forged = token_module._encode(
        SECRET,
        TokenPayload(
            scope=Scope.CLIENT,
            sub="42",
            iat=NOW - timedelta(days=100),
            exp=NOW + timedelta(days=1),
        ),
    )

    with pytest.raises(InvalidTokenError):
        renew(SECRET, forged, scope=Scope.CLIENT, now=NOW)


def test_verification_enforces_the_absolute_limit_even_if_the_expiry_is_fine() -> None:
    """验证侧对绝对上限**再判一次**，不只依赖签发侧的截断。

    这里手工签一个「iat 是 100 天前、exp 却还没到」的 token —— 正常路径签不出
    它，但任何一个新的签发入口写漏了截断就会。兜底在验证侧，那是所有路径的必经
    之地。
    """
    long_ago = NOW - timedelta(days=100)
    forged = token_module._encode(
        SECRET,
        TokenPayload(scope=Scope.CLIENT, sub="42", iat=long_ago, exp=NOW + timedelta(days=1)),
    )

    with pytest.raises(InvalidTokenError):
        verify(SECRET, forged, scope=Scope.CLIENT, now=NOW)


def test_the_absolute_limit_does_not_apply_to_operator_tokens() -> None:
    """上一条的对照组：同样是「iat 在 100 天前」，运营 token 该照常放行。

    运营不需要那条线 —— 重新登录一次就行，而它的单次有效期本来就只有 8 小时。
    两条合起来才说明这个上限**只对客户端**生效，而不是对所有 token 生效。
    """
    forged = token_module._encode(
        SECRET,
        TokenPayload(
            scope=Scope.OPERATOR,
            sub="admin",
            iat=NOW - timedelta(days=100),
            exp=NOW + OPERATOR_TOKEN_TTL,
        ),
    )

    assert verify(SECRET, forged, scope=Scope.OPERATOR, now=NOW).sub == "admin"


def test_an_empty_secret_refuses_to_work_rather_than_signing_with_nothing() -> None:
    """空密钥**签得出也验得过** —— 那样系统看起来一切正常，实际上等于没有认证。

    所以它必须是一个**区别于「token 无效」的**错误：部署方缺了配置项，不是
    调用方密码错了。接口层据此回 503 而不是 401。
    """
    empty = SecretStr("")

    with pytest.raises(AuthNotConfiguredError):
        issue(empty, scope=Scope.CLIENT, sub="42", now=NOW)
    with pytest.raises(AuthNotConfiguredError):
        verify(empty, "v1.a.b", scope=Scope.CLIENT, now=NOW)


def test_client_id_of_a_non_numeric_subject_is_an_invalid_token() -> None:
    """签名合法但 sub 不是数字，只可能是我们自己签坏了 —— 也不该炸成 500。"""
    forged = token_module._encode(
        SECRET,
        TokenPayload(scope=Scope.CLIENT, sub="不是数字", iat=NOW, exp=NOW + CLIENT_TOKEN_TTL),
    )
    payload = verify(SECRET, forged, scope=Scope.CLIENT, now=NOW)

    with pytest.raises(InvalidTokenError):
        _ = payload.client_id


def test_operator_payload_refuses_to_pose_as_a_client() -> None:
    payload = verify(
        SECRET,
        issue(SECRET, scope=Scope.OPERATOR, sub="admin", now=NOW)[0],
        scope=Scope.OPERATOR,
        now=NOW,
    )

    with pytest.raises(InvalidTokenError):
        _ = payload.client_id


def test_signature_is_compared_in_constant_time_and_before_anything_is_parsed() -> None:
    """这一条扫的是**源码结构**，不是行为。

    「`==` 比对签名」和「先解析后验签」都没有可观察的行为差异 —— 两种写法的
    输入输出完全一样，差别只在时序侧信道和攻击面。测不出来的东西就只能盯着代码
    形状，这和 `test_migration_safety.py` 扫 `upgrade()` 里的 drop 是同一个路子。
    """
    source = inspect.getsource(token_module.verify)

    assert "compare_digest" in source, "签名比对必须用 hmac.compare_digest，`==` 会泄露比对进度"
    for written_with_equals in ("== signature", "signature ==", "== expected", "expected =="):
        assert written_with_equals not in source, (
            f"签名被 `{written_with_equals}` 比了 —— `==` 逐字节短路，会泄露比对进度"
        )
    assert source.index("compare_digest") < source.index("_decode_payload"), (
        "解析排到了验签前面 —— 那等于开了一条不验签也能走的解析路径"
    )
    assert source.index("compare_digest") < source.index("payload.exp"), (
        "过期判定排到了验签前面，同上"
    )
