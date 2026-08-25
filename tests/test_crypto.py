"""平台凭据的加解密。

**这里验的四件事，错了都不会报错、只会静默地让加密失效**：空密钥照样能加密、
密文被改动后照样能解、换过密钥之后给出一个空值、以及拿密文当身份比对。

一个数据库都不起 —— `auth/` 是纯计算层，这正是把它压在分层图底部的回报。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from adpilot.auth import crypto

# 32 个字符，正好卡在下限上（`openssl rand -base64 32` 出来的串在这个量级之上）。
SECRET = SecretStr("0123456789abcdef0123456789abcdef")
OTHER_SECRET = SecretStr("fedcba9876543210fedcba9876543210")
TOKEN = SecretStr("act.example-tiktok-access-token")


def test_roundtrip_returns_the_original_token() -> None:
    encrypted = crypto.encrypt(SECRET, TOKEN)

    assert crypto.decrypt(SECRET, encrypted).get_secret_value() == TOKEN.get_secret_value()


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    """密文里不能有明文的影子。

    看起来是句废话，但它挡的是一类真实的实现事故：拿 base64 或者异或当「加密」，
    那种东西在肉眼看不出问题，而 dump 泄露时和明文没有区别。
    """
    encrypted = crypto.encrypt(SECRET, TOKEN)

    assert TOKEN.get_secret_value() not in encrypted


def test_the_same_token_encrypts_differently_every_time() -> None:
    """同一段明文两次加出来的密文不同（Fernet 每次换 IV）。

    🔴 **推论：永远不要拿密文去比对「是不是同一个 token」** —— 那种比较永远返回
    「不同」，而写出这种比较的人会得到一个「每次都要重新授权」的系统。
    """
    assert crypto.encrypt(SECRET, TOKEN) != crypto.encrypt(SECRET, TOKEN)


def test_a_different_secret_cannot_decrypt() -> None:
    """换了 `CREDENTIALS_SECRET` 之后，老密文就解不开了。

    这正是那条「密钥丢了所有凭据全部作废」的机器形态。它必须**抛异常**而不是
    返回空串 —— 后者会让系统拿着一个空 token 去请求平台，然后得到一个和
    「密钥换过了」毫无关系的报错。
    """
    encrypted = crypto.encrypt(SECRET, TOKEN)

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(OTHER_SECRET, encrypted)


def test_tampered_ciphertext_is_rejected() -> None:
    """密文被改过一个字节就解不开 —— 这是选带认证的 Fernet 而不是裸 AES 的理由。

    没有这一层的话，改动密文得到的是一段**乱码明文**，而系统会拿着那段乱码去
    当 token 用。
    """
    encrypted = crypto.encrypt(SECRET, TOKEN)
    # 动最后一个字符（版本前缀之后的正文）。
    tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(SECRET, tampered)


@pytest.mark.parametrize("secret", [SecretStr(""), SecretStr("short"), SecretStr("a" * 31)])
def test_missing_or_short_secret_refuses_to_work(secret: SecretStr) -> None:
    """🔴 空的或过短的密钥必须**拒绝工作**，不能退化成「用空串当密钥」。

    退化的后果是：系统看起来一切正常（加得出、解得开），实际上等于没有加密。
    这正是 CLAUDE.md 硬规矩 2 说的那种失败 —— 而它没有任何症状。
    """
    with pytest.raises(crypto.CryptoNotConfiguredError):
        crypto.encrypt(secret, TOKEN)


def test_unknown_ciphertext_format_is_rejected() -> None:
    """没有版本前缀的东西一律拒绝。

    这条护的是将来换算法那一天：新旧密文靠前缀区分，而一个「没前缀也试着解一下」
    的实现会让那次迁移变成猜谜。
    """
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(SECRET, "这不是本模块加密出来的东西")
