"""运营密码的哈希与比对。

这些用例真的会跑 argon2（每次几十毫秒，故意的），所以数量克制 —— 验的是失败
路径怎么失败，不是把库再测一遍。
"""

from __future__ import annotations

from adpilot.auth.password import hash_password, verify_operator, verify_password

# 假密码，不是任何地方的真实凭据。
PASSWORD = "test-not-a-secret-password"


def test_a_correct_password_verifies() -> None:
    assert verify_password(hash_password(PASSWORD), PASSWORD)


def test_a_wrong_password_does_not() -> None:
    assert not verify_password(hash_password(PASSWORD), PASSWORD + "x")


def test_an_unset_hash_lets_nobody_in() -> None:
    """没配 `OPERATOR_PASSWORD_HASH` 时，**谁也进不来**。

    这是安全方向的失败，和「空密码谁都能进」正好相反 —— 后者才是 CLAUDE.md
    硬规矩 2 要挡的东西。这条用例是那个取舍的锚点：哪天有人给它加了「空哈希
    就放行」的兜底，这里会红。
    """
    assert not verify_password("", PASSWORD)
    assert not verify_password("", "")


def test_a_corrupted_hash_is_a_failed_login_not_a_crash() -> None:
    """`.env` 里的哈希被截断过（比如少了单引号、被 compose 展开吃掉半截）。

    结果必须是登录失败，不是 500 —— 后者会把「配置写坏了」变成一个看起来像
    服务故障的现象。
    """
    assert not verify_password("$argon2id$v=19$m=65536", PASSWORD)
    assert not verify_password("显然不是哈希", PASSWORD)


def test_the_hash_neither_contains_nor_repeats_the_password() -> None:
    """带盐：同一个密码两次哈希不同，且哈希里看不到明文。"""
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert PASSWORD not in first
    assert first != second


def test_operator_check_needs_both_halves() -> None:
    """用户名和密码任何一半错都进不去。"""
    password_hash = hash_password(PASSWORD)
    correct = {"expected_username": "admin", "password_hash": password_hash}

    assert verify_operator(username="admin", password=PASSWORD, **correct)
    assert not verify_operator(username="admin", password="错的", **correct)
    assert not verify_operator(username="别人", password=PASSWORD, **correct)
    assert not verify_operator(username="别人", password="错的", **correct)
