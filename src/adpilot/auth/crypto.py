"""对称加密：给落库的平台凭据用。

## 为什么凭据要加密，而数据库密码不用

数据库密码只活在 `.env` 里。平台 token 不一样 —— 它是**运行时产物**（人在后台点
授权、平台回调过来才有），只能落库。而**数据库 dump 会被拷来拷去**：备份、迁移、
本地复现一个线上问题。一份明文 token 的 dump，等于把广告账户的读取权限装进了一个
会被邮件传来传去的文件。

⚠️ **加密不防「有人同时拿到数据库和 env」** —— 那时两样都在他手上。它防的是这两者
**分头泄露**，而分头泄露恰恰是最常见的形态：dump 传出去了、env 没有；日志里带出了
env、数据库没有。

## 🔴 `CREDENTIALS_SECRET` 丢了 = 所有 token 全部解不开

这一点和 `AUTH_SECRET` 有本质区别：后者丢了只是所有人重新登录一次，前者丢了要把
每个平台的授权流程重走一遍。所以它必须进凭据存档，不能只活在部署机的 `.env` 里。

## 为什么是 Fernet，为什么密钥用 HKDF 派生

Fernet 是 AES-128-CBC + HMAC-SHA256 的成品组合，**带认证**：密文被改过一个字节
就解不开，而不是解出一段乱码。自己拼 AES 的人十有八九会漏掉这一层，然后得到一个
「能解密但内容可被篡改」的东西。

密钥不直接拿 `CREDENTIALS_SECRET` 的字节用，而是走 HKDF 派生，理由是 `info`
那个参数：它把「这把密钥是干什么用的」编进派生过程。将来若有第二种加密用途，
换一个 `info` 就是另一把密钥，同一个 secret 不会在两处产出同一把钥匙。

**不用 PBKDF2 / argon2 这类慢哈希**是有意的：那类算法防的是「人选的弱口令被离线
爆破」，而这里的输入是 `openssl rand -base64 32` 出来的高熵随机串。对它做十万轮
迭代只是让每次解密变慢，不增加任何安全性。（用户密码那条路走的是 argon2，见
`password.py` —— 两者防的不是同一件事。）
"""

from __future__ import annotations

import base64
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

#: 密文的版本前缀。换算法时新旧并存一段时间，而不是让所有凭据同时失效 ——
#: 和 `token.py` 的 `v1.` 是同一个思路。
_VERSION: Final = "v1"

#: HKDF 的用途标签。**改它等于换钥匙**：所有已存的密文都会解不开。
_INFO: Final = b"adpilot-credentials-v1"

#: 密钥最小长度。和 `AUTH_SECRET` 同一个量级，理由也一样 —— 短密钥是离线爆破的
#: 活靶子，而 `openssl rand -base64 32` 出来的串正好在这个量级之上。
SECRET_MIN_LENGTH: Final = 32


class CryptoNotConfiguredError(Exception):
    """没配 `CREDENTIALS_SECRET`，或者配得太短。

    与「解不开」分开，因为**这是部署方的问题，不是数据的问题** —— 回一句
    「凭据损坏」会让人去怀疑数据库，而真正该做的是去填一个环境变量。
    """


class DecryptionError(Exception):
    """解不开。

    只可能是三种情况：`CREDENTIALS_SECRET` 换过了、密文被改过、或者存进去的
    根本不是本模块加密的东西。**不区分**是哪一种 —— 三者的处置完全一样：
    重新走一次授权。
    """


def encrypt(secret: SecretStr, plaintext: SecretStr) -> str:
    """加密，返回可直接落库的字符串。

    同一段明文每次加出来的密文都不同（Fernet 每次用新的 IV），所以**不要拿密文
    去比对「是不是同一个 token」** —— 那种比较永远返回「不同」。
    """
    token = _fernet(secret).encrypt(plaintext.get_secret_value().encode())
    return f"{_VERSION}.{token.decode()}"


def decrypt(secret: SecretStr, ciphertext: str) -> SecretStr:
    """解密。任何问题都抛 `DecryptionError`。

    返回 `SecretStr` 而不是裸 `str`：解出来的东西是凭据，不该因为一次日志、
    一次异常栈或一次 `repr()` 就泄出去（同 `config.py` 的规矩 2）。
    """
    version, _, payload = ciphertext.partition(".")
    if version != _VERSION or not payload:
        raise DecryptionError("密文格式不认识")

    try:
        plaintext = _fernet(secret).decrypt(payload.encode())
    except (InvalidToken, ValueError) as exc:
        # 不把底层异常的文本带出去 —— 那里面可能有密文片段。
        raise DecryptionError("凭据解不开：密钥换过了，或者数据被改动过") from exc

    return SecretStr(plaintext.decode())


def _fernet(secret: SecretStr) -> Fernet:
    raw = secret.get_secret_value()
    if len(raw) < SECRET_MIN_LENGTH:
        # 空的或太短的密钥**加得出也解得开**，那样系统看起来一切正常，实际上
        # 等于没有加密。所以这里不是「用个默认值顶上」，是拒绝工作
        # （CLAUDE.md 硬规矩 2）。
        raise CryptoNotConfiguredError(
            f"未配置 CREDENTIALS_SECRET 或长度不足 {SECRET_MIN_LENGTH} 个字符"
        )

    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        # 不加 salt：salt 得和密文一起存，而每条凭据存一份 salt 只在「防彩虹表」
        # 时有意义 —— 那是口令场景的问题，不是高熵随机密钥的问题。用途隔离由
        # `info` 承担。
        salt=None,
        info=_INFO,
    ).derive(raw.encode())

    return Fernet(base64.urlsafe_b64encode(derived))
