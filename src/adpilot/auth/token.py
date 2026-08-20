"""自签的 HMAC token。

格式：

    v1.<base64url(payload)>.<base64url(hmac_sha256(payload, AUTH_SECRET))>

**为什么不用 JWT。** JWT 的灵活性这里一样都用不上（不需要多算法、不需要跨服务
验签、不需要第三方签发），而它的复杂度带来了真实的攻击面：`alg: none`、
HS256/RS256 混淆，根源都是「算法由 token 自己声明」这个设计。我们只有一个算法、
写死在下面，那一类攻击就不存在。完整理由见
[设计文档第四节](../../../docs/design/2026-08-21-client-auth.md)。

`v1.` 前缀是给将来留的路：换算法时新旧并存一段时间，而不是让所有人同时失效。

🔴 **这个模块里有四件事错了不会报错、只会悄悄放行**，每件都有测试盯着：

1. **签名比对必须 `hmac.compare_digest`。** `==` 逐字节短路，比对耗时会泄露
   「猜对了几个字节」。
2. **先验签、再解析 payload。** 顺序反了等于开了一个不验签的解析入口 —— 攻击者
   随便编一个 payload 就能让服务端按它的内容走一段逻辑。
3. **验签对收到的原始字节做**，不是「解析成对象再重新序列化一遍」。JSON 的字节
   形态不唯一（键序、空格、转义），重新序列化出来的东西和签名覆盖的东西不是同
   一串。
4. **`scope` 必须比对。** 只验签名的话，一个合法的客户端 token 就能直接调运营
   接口 —— 签名是真的，只是拿错了门。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import SecretStr

_VERSION: Final = "v1"

# 单次有效期。客户端长、运营短 —— 运营 token 的权限大得多：它能导数据、能改客户。
CLIENT_TOKEN_TTL: Final = timedelta(days=7)
OPERATOR_TOKEN_TTL: Final = timedelta(hours=8)

# 客户端的绝对上限，从**首次签发**算起。滑动过期必须有这么一条封顶线：没有它，
# 一个 token 可以无限续下去，等于永久凭据 —— 而「客户换了手机、旧手机被卖掉」
# 这类事就再没有任何办法处理。
CLIENT_TOKEN_MAX_AGE: Final = timedelta(days=90)


class Scope(StrEnum):
    """token 能进哪扇门。

    两套认证换出来的是同一种 token，只是 scope 不同 —— 而**校验时必须比对它**，
    否则「同一种 token」就成了「同一把钥匙」。
    """

    CLIENT = "client"
    OPERATOR = "operator"


class InvalidTokenError(Exception):
    """token 不可信：格式不对、签名不对、过期了，或者拿错了门。

    **不区分具体是哪一种。** 区分等于告诉攻击者他走到了哪一步 —— 而对合法用户
    来说这三种情况的处置完全一样：重新登录。

    不继承 `DomainError`：那一族属于 `services/`，而这一层够不着它
    （见本包的 `__init__.py`）。翻成 401 是 `api/deps.py` 的事。
    """


class AuthNotConfiguredError(Exception):
    """没配 `AUTH_SECRET`。

    与上面那个分开，因为**它是部署方的问题，不是调用方的问题** —— 回 401 会让
    人以为是密码错了，然后对着一个永远进不去的登录框试半天。`api/` 层把它翻成
    503 并说清缺了哪个配置项。
    """


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """token 里那四个字段。

    ⚠️ **`iat` 是「首次签发」，不是「本次签发」** —— 续期时它原样保留，只推
    `exp`。绝对上限（`iat + CLIENT_TOKEN_MAX_AGE`）正是靠它算出来的，重置一次
    就等于把上限往后挪了一次，那条线也就形同虚设了。
    """

    scope: Scope
    sub: str
    iat: datetime
    exp: datetime

    @property
    def client_id(self) -> int:
        """客户端 token 里的 `sub` 是 `client_id`。

        解析放在这里而不是让调用方 `int(payload.sub)`：一个签名合法但 `sub` 是
        `"abc"` 的 token（只可能来自我们自己签坏了）不该在 handler 里炸成 500。
        """
        if self.scope is not Scope.CLIENT:
            raise InvalidTokenError("不是客户端 token")
        try:
            return int(self.sub)
        except ValueError as exc:
            raise InvalidTokenError("客户端 token 的 sub 不是客户 ID") from exc


def issue(
    secret: SecretStr,
    *,
    scope: Scope,
    sub: str,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """签发一个新 token，返回 (token, 到期时间)。"""
    now = _now(now)
    exp = now + _ttl(scope)
    return _encode(secret, TokenPayload(scope=scope, sub=sub, iat=now, exp=exp)), exp


def renew(
    secret: SecretStr,
    token: str,
    *,
    scope: Scope,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """拿一个**尚未过期**的 token 换一个新的，返回 (token, 到期时间)。

    客户端做滑动过期：常看的人永远不用重扫，不活跃的自然失效。但新的 `exp`
    要在绝对上限那里截断 —— `min(now + 7 天, iat + 90 天)`。到了上限就只能
    重新扫码，这是这个方案里唯一的「强制重新认证」时刻。
    """
    now = _now(now)
    payload = verify(secret, token, scope=scope, now=now)

    exp = now + _ttl(scope)
    if scope is Scope.CLIENT:
        # 截断到上限。**不必再判「截断后是不是已经过期」** —— 上面那句 `verify`
        # 已经保证了 `now < iat + CLIENT_TOKEN_MAX_AGE`（它自己也判这条线），
        # 所以截出来的 exp 一定还在未来。真正拒绝「续过头」的地方是 verify，
        # 那是所有路径的必经之地；在这里再写一个分支只会是永远走不到的死代码。
        exp = min(exp, payload.iat + CLIENT_TOKEN_MAX_AGE)

    return _encode(
        secret, TokenPayload(scope=scope, sub=payload.sub, iat=payload.iat, exp=exp)
    ), exp


def verify(
    secret: SecretStr,
    token: str,
    *,
    scope: Scope,
    now: datetime | None = None,
) -> TokenPayload:
    """校验并解出 payload，任何问题都抛 `InvalidTokenError`。

    步骤的**顺序**就是这个函数的全部内容，见模块 docstring 的四条。
    """
    now = _now(now)
    key = _key(secret)

    try:
        version, payload_part, signature_part = token.split(".")
    except ValueError as exc:
        raise InvalidTokenError("token 格式不对") from exc

    if version != _VERSION:
        raise InvalidTokenError("token 版本不认识")

    payload_bytes = _b64decode(payload_part)
    signature = _b64decode(signature_part)

    # ① 先验签。验的是**收到的那串字节**，不是解析回来再拼一遍的结果。
    # ② compare_digest 而不是 == —— 后者逐字节短路，会泄露比对进度。
    expected = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidTokenError("签名不对")

    payload = _decode_payload(payload_bytes)

    # ③ 拿错门也是不可信。合法签名 + 错的 scope，正是「客户端 token 调运营接口」
    # 这个越权的形状。
    if payload.scope is not scope:
        raise InvalidTokenError("token 作用域不对")

    # ④ 过期判定排在验签之后：先判过期的话，就有了一条不验签也能走的解析路径。
    if payload.exp <= now:
        raise InvalidTokenError("token 已过期")

    # 绝对上限在这里**再判一次**（签发侧已经截断过）。重复是有意的：这条线是
    # 「客户换了手机」唯一的兜底，只靠签发侧的话，任何一个新的签发入口写漏了都
    # 会静默地把它变成永久凭据。
    if payload.scope is Scope.CLIENT and now >= payload.iat + CLIENT_TOKEN_MAX_AGE:
        raise InvalidTokenError("超过绝对有效期上限")

    return payload


def _now(now: datetime | None) -> datetime:
    """当前时间。**一律 aware**（`conventions.md` 的时区一节：不用 `utcnow()`）。

    做成可注入的参数，是为了让「过期」「续到上限」这些用例不必真的等 90 天。
    """
    return now or datetime.now(UTC)


def _ttl(scope: Scope) -> timedelta:
    return CLIENT_TOKEN_TTL if scope is Scope.CLIENT else OPERATOR_TOKEN_TTL


def _key(secret: SecretStr) -> bytes:
    raw = secret.get_secret_value()
    if not raw:
        # 空密钥**签得出也验得过** —— 那样系统看起来一切正常，实际上等于没有
        # 认证。所以这里不是「用个空串顶上」，是拒绝工作。
        raise AuthNotConfiguredError("未配置 AUTH_SECRET")
    return raw.encode()


def _encode(secret: SecretStr, payload: TokenPayload) -> str:
    key = _key(secret)
    payload_bytes = json.dumps(
        {
            "scope": payload.scope.value,
            "sub": payload.sub,
            "iat": int(payload.iat.timestamp()),
            "exp": int(payload.exp.timestamp()),
        },
        # 紧凑且键序固定。**不是为了省字节** —— 是为了让同一个 payload 每次都
        # 编出同一串字节，否则「签的」和「验的」可能不是同一个东西。
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    return f"{_VERSION}.{_b64encode(payload_bytes)}.{_b64encode(signature)}"


def _decode_payload(raw: bytes) -> TokenPayload:
    try:
        data = json.loads(raw)
        return TokenPayload(
            scope=Scope(data["scope"]),
            sub=str(data["sub"]),
            iat=datetime.fromtimestamp(data["iat"], tz=UTC),
            exp=datetime.fromtimestamp(data["exp"], tz=UTC),
        )
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        # 走到这里说明签名是对的、内容却不合法 —— 只可能是我们自己签坏了，或者
        # 密钥泄了。两种都不该把异常细节回给调用方。
        raise InvalidTokenError("token 内容不合法") from exc


def _b64encode(raw: bytes) -> str:
    """base64url，**去掉填充的 `=`**。

    去 padding 是因为 token 会出现在 URL、二维码和小程序的存储里，而 `=` 在这几
    个地方各有各的转义习惯。解码那侧自己补回来。
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64decode(part: str) -> bytes:
    padding = "=" * (-len(part) % 4)
    try:
        return base64.urlsafe_b64decode(part + padding)
    except (binascii.Error, ValueError) as exc:
        raise InvalidTokenError("token 编码不合法") from exc
