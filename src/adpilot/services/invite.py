"""邀请码的业务逻辑：生成、列出、作废、兑换。

**兑换（`redeem`）是这条链上唯一不需要认证就能打的入口**，所以它的失败路径要
一律长得一样 —— 见那个函数的 docstring。
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.client import Client
from adpilot.models.invite import Invite
from adpilot.services import client as client_service
from adpilot.services.exceptions import NotFoundError

#: 邀请码的默认有效期。运营可以在生成时指定别的，但**不能不限期** ——
#: 一个永不过期的码等于给这个客户的数据配了一把永久钥匙。
DEFAULT_TTL = timedelta(days=30)
MAX_TTL = timedelta(days=365)

#: 随机字节数。24 字节 = 192 位熵，`token_urlsafe` 出来是 32 个字符。
#: **不要往下调**：这串东西是不需要认证就能试的，熵少了就有了爆破的余地。
CODE_BYTES = 24

#: 兑换失败一律是这一句。见 `redeem`。
_INVALID_CODE = "邀请码无效、已过期或已作废"


def hash_code(code: str) -> str:
    """码 → 存进库的哈希。

    SHA-256 而不是 argon2：这里的输入是 192 位随机串，不是人类密码 —— 慢哈希
    防不住任何本来防得住的攻击，只会让每次兑换白等几十毫秒。完整对照见
    `auth/password.py` 的模块 docstring。
    """
    return hashlib.sha256(code.encode()).hexdigest()


async def create(
    session: AsyncSession,
    *,
    client_id: int,
    ttl: timedelta = DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[Invite, str]:
    """给某个客户生成一个邀请码，返回 (行, **明文码**)。

    明文只有这一次能拿到 —— 库里存的是哈希，谁也还原不回去。调用方要么当场渲染
    成二维码，要么就只能重新生成一个。
    """
    now = now or datetime.now(UTC)
    # 客户不存在时抛 NotFoundError（路径上指名的资源不存在 → 404），这与
    # `ad_accounts` 那边把 client_id 当**请求体字段**校验、抛 ReferenceNotFoundError
    # 不同：那里该改请求体，这里该换 URL。
    await client_service.get(session, client_id)

    code = secrets.token_urlsafe(CODE_BYTES)
    invite = Invite(
        client_id=client_id,
        code_hash=hash_code(code),
        expires_at=now + min(ttl, MAX_TTL),
    )
    session.add(invite)
    await session.flush()
    return invite, code


async def list_for_client(session: AsyncSession, *, client_id: int) -> Sequence[Invite]:
    """列出一个客户的全部邀请码，新的在前。**不含明文码**（库里就没有）。

    不分页：一个客户身上的码是个位数，分页只会让内部后台多写一个组件。
    """
    await client_service.get(session, client_id)
    rows = await session.scalars(
        select(Invite).where(Invite.client_id == client_id).order_by(Invite.id.desc())
    )
    return rows.all()


async def revoke(
    session: AsyncSession,
    *,
    client_id: int,
    invite_id: int,
    now: datetime | None = None,
) -> Invite:
    """作废一个邀请码。**已经换出去的 token 不受影响**（自包含，最多再活 7 天）。

    重复作废是幂等的：第二次不改 `revoked_at`，免得「什么时候断的」这个时间戳被
    后一次点击覆盖掉。

    `client_id` 是必填的，且参与查询条件 —— 路径是
    `/clients/{client_id}/invites/{invite_id}`，拿别的客户的 invite_id 来试要 404
    而不是「作废成功」。
    """
    now = now or datetime.now(UTC)
    invite = await session.scalar(
        select(Invite).where(Invite.id == invite_id, Invite.client_id == client_id)
    )
    if invite is None:
        raise NotFoundError(f"邀请码不存在：{invite_id}")

    if invite.revoked_at is None:
        invite.revoked_at = now
        await session.flush()
    return invite


async def redeem(
    session: AsyncSession,
    code: str,
    *,
    now: datetime | None = None,
) -> Client:
    """拿明文码换出它对应的客户，顺手记一次使用。

    🔴 **四种失败回同一个 `NotFoundError`**：码不存在、过期了、被作废了、客户已
    停止合作。这个接口不需要认证就能打，分开报错等于告诉试码的人「这个码是真的，
    只是过期了」—— 而对合法的客户来说，这四种情况的处置完全一样：找运营要个新的。

    按哈希查是一次索引命中，不会因为码对不对而快慢不同。
    """
    now = now or datetime.now(UTC)
    invite = await session.scalar(select(Invite).where(Invite.code_hash == hash_code(code)))
    if invite is None or invite.revoked_at is not None or invite.expires_at <= now:
        raise NotFoundError(_INVALID_CODE)

    client = await session.get(Client, invite.client_id)
    if client is None or not client.is_active:
        # 停止合作的客户，码也跟着失效 —— 否则「不再服务这个客户」这件事要靠人
        # 记得去逐个作废他手上的码。
        raise NotFoundError(_INVALID_CODE)

    invite.last_used_at = now
    # 在数据库里自增，不是 `invite.use_count + 1`：两个人同时扫码的话，读改写会
    # 丢掉其中一次。这个数字本身不重要，但「读出来加一再写回去」是个会被抄走的
    # 坏形状。
    invite.use_count = Invite.use_count + 1
    await session.flush()
    return client
