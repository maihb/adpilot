"""运营密码的哈希与比对。

**为什么这里用 argon2，而邀请码那边用 SHA-256**（`services/invite.py`）：argon2
慢是它的功能，用来抵消人类密码的低熵；邀请码是 192 位随机串，对它做慢哈希防不住
任何本来防得住的攻击，只是让每次兑换白等几十毫秒。两处用不同的哈希是刻意的。

🔴 **`verify` 是 CPU 密集的（几十毫秒，故意的）。** 在 `async def` 里直接调会把
整个事件循环卡住 —— 症状是**全局变慢**，不是登录变慢
（[conventions.md](../../../docs/code-rules/conventions.md) 的异步一节）。调用方
一律 `await asyncio.to_thread(...)`。

生成哈希：

    uv run python -m adpilot.auth.password
"""

from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

# 用库的默认参数（RFC 9106 的低内存档）。**不要自己调低** —— 调参数唯一的正当
# 理由是机器扛不住，而这个系统的登录频率是「一两个人每天几次」。
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """算出可以写进 `.env` 的哈希串。"""
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """比对密码。哈希为空或格式不对时返回 `False`，不抛。

    空哈希（没配 `OPERATOR_PASSWORD_HASH`）落在这里的结果是**谁也进不来**，
    这是安全方向的失败 —— 与「空密码谁都能进」正好相反，后者才是
    [CLAUDE.md](../../../CLAUDE.md) 硬规矩 2 要挡的东西。
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, plain)
    except (Argon2Error, UnicodeEncodeError, TypeError):
        # 密码不对、哈希串损坏，对调用方是同一件事：登录失败。
        #
        # ⚠️ `UnicodeEncodeError` 不是多余的：argon2 内部把哈希串按 **ascii**
        # 编码，一个非 ASCII 字符（配置粘错了、被编辑器替换了引号）会在那里炸，
        # 而不是走 `Argon2Error`。放过它的话，「.env 写坏了」就会表现成 500 ——
        # 一个看起来像服务故障、实际是配置问题的现象。
        return False


def verify_operator(
    *,
    username: str,
    password: str,
    expected_username: str,
    password_hash: str,
) -> bool:
    """运营账号的完整比对。

    用户名也走 `compare_digest`：`==` 在第一个不同的字节就返回，比对耗时会泄露
    「猜对了几个字符」。用户名不算机密，但这行代码的成本是零。

    **不管哪一半错，都要把两半都跑完**（先算完再取与），否则「用户名对了、密码
    错了」和「用户名就不对」在响应时间上分得出来 —— 前者慢几十毫秒，因为它真的
    去算了一次 argon2。
    """
    name_ok = hmac.compare_digest(username.encode(), expected_username.encode())
    password_ok = verify_password(password_hash, password)
    return name_ok and password_ok


def _main() -> None:
    """交互式生成一个哈希。

    **密码只从 `getpass` 读，不接受命令行参数** —— 参数会原样进 shell history，
    而那个文件既不加密也常常被同步到别处。
    """
    import getpass
    import sys

    plain = getpass.getpass("运营密码：")
    if not plain:
        sys.exit("密码不能为空")
    if plain != getpass.getpass("再输一遍："):
        sys.exit("两次输入不一致")

    # 单引号不能省：argon2 的哈希串长这样 `$argon2id$v=19$m=65536,...`，而
    # **docker compose 读 .env 时会对不带引号的值做变量展开** —— `$argon2id`
    # 会被当成一个未定义的变量替换成空串，于是容器里的哈希只剩半截。症状是
    # 「本机登录得了，compose 起的那套登录不了」。单引号在 compose 和
    # python-dotenv 两边都表示「原样取用」。
    print("\n把这一行写进 .env（单引号不能省，理由见本命令的源码）：\n")
    print(f"OPERATOR_PASSWORD_HASH='{hash_password(plain)}'")


if __name__ == "__main__":
    _main()
