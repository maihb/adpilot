"""广告账户的业务逻辑。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.ad_account import DEFAULT_REPORT_DELAY_HOURS, AdAccount, Platform
from adpilot.models.client import Client
from adpilot.models.fetch import PlatformCredential
from adpilot.services.exceptions import (
    ConflictError,
    NotFoundError,
    ReferenceNotFoundError,
)

# `platform` 与 `external_id` 不在里面：它们是账户的身份。改了等于换了一个账户，
# 而历史 daily_metrics 仍挂在原 account_id 上，改完那些数据就跟平台对不上了。
#: 🔴 **加了可改的字段就要在这里补一个名字。** `update` 拿它当白名单，漏了的
#: 症状是接口收下了那个字段、返回 200、而值根本没变 —— 一次安静的无操作。
UPDATABLE_FIELDS = frozenset(
    {
        "client_id",
        "name",
        "currency",
        "timezone",
        "is_active",
        "auto_report",
        "report_delay_hours",
    }
)


async def create(
    session: AsyncSession,
    *,
    client_id: int,
    platform: Platform,
    external_id: str,
    name: str,
    currency: str,
    timezone: str,
    auto_report: bool = True,
    report_delay_hours: int = DEFAULT_REPORT_DELAY_HOURS,
) -> AdAccount:
    """建一个广告账户。

    客户不存在抛 `ReferenceNotFoundError`，`(platform, external_id)` 重复抛
    `ConflictError`。

    ⚠️ 两个日报开关在这里**显式传**，不靠列的 server_default 兜 —— 靠默认值的话
    调用方传了 `auto_report=False` 也不会有任何效果（ORM 不会把它写进 INSERT），
    而接口会照常返回 201。
    """
    await _ensure_client_exists(session, client_id)

    account = AdAccount(
        client_id=client_id,
        platform=platform,
        external_id=external_id,
        name=name,
        currency=currency,
        timezone=timezone,
        auto_report=auto_report,
        report_delay_hours=report_delay_hours,
    )
    session.add(account)
    await _flush_or_conflict(session, platform=platform, external_id=external_id)
    return account


async def get(session: AsyncSession, account_id: int) -> AdAccount:
    """按 ID 取账户，不存在抛 `NotFoundError`。"""
    account = await session.get(AdAccount, account_id)
    if account is None:
        raise NotFoundError(f"广告账户不存在：{account_id}")
    return account


async def get_for_client(session: AsyncSession, account_id: int, *, client_id: int) -> AdAccount:
    """取一个账户，**且它必须属于这个客户**；不属于就跟不存在一样抛 `NotFoundError`。

    🔴 **客户端那条路径上的每一次「按 account_id 查」都必须先过这里。** 它是
    `/api/portal/*` 上唯一的越权入口 —— 路径里带着一个别人的 ID，而别的入参都
    来自 token。

    404 而不是 403：403 等于承认「这个账户存在，只是不给你看」，那本身就是一条
    情报。对合法调用方来说两者没有区别，他本来就查不到不属于自己的账户。

    `client_id` 是**必填关键字参数**，没有默认值 —— 这是设计文档那两层机器保证的
    第二层：忘了传就根本调不通，而不是「忘了传就查到全部」。
    """
    account = await session.scalar(
        select(AdAccount).where(AdAccount.id == account_id, AdAccount.client_id == client_id)
    )
    if account is None:
        raise NotFoundError(f"广告账户不存在：{account_id}")
    return account


async def list_page(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    client_id: int | None = None,
    platform: Platform | None = None,
    is_active: bool | None = None,
) -> tuple[Sequence[AdAccount], int]:
    """分页列出账户，返回 (本页, 过滤后总数)。排序理由同 `client.list_page`。

    注意返回的对象**没有 eager load `client`** —— `AdAccount.client` 声明了
    `lazy="raise"`，在出参里碰它会当场报错而不是偷偷发一条查询。响应 schema 里
    只有 `client_id`，所以用不着；真要带客户名，得在这里显式 `selectinload`。
    """
    filters = []
    if client_id is not None:
        filters.append(AdAccount.client_id == client_id)
    if platform is not None:
        filters.append(AdAccount.platform == platform)
    if is_active is not None:
        filters.append(AdAccount.is_active == is_active)

    total = await session.scalar(select(func.count(AdAccount.id)).where(*filters))
    rows = await session.scalars(
        select(AdAccount)
        .where(*filters)
        .order_by(AdAccount.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def update(
    session: AsyncSession,
    account_id: int,
    changes: Mapping[str, object],
) -> AdAccount:
    """按 `changes` 里出现的键更新账户；没出现的键保持不动。

    改 `currency` / `timezone` **不会重算已有的 `daily_metrics`** —— 那些行记的
    是当时的口径。换了口径要重跑归一化，拿 Mongo 里的原始快照重算。
    """
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"不可更新的字段：{sorted(unknown)}")

    account = await get(session, account_id)

    new_client_id = changes.get("client_id")
    if isinstance(new_client_id, int):
        await _ensure_client_exists(session, new_client_id)

    for field, value in changes.items():
        setattr(account, field, value)

    await _flush_or_conflict(
        session,
        platform=account.platform,
        external_id=account.external_id,
    )
    return account


async def _ensure_client_exists(session: AsyncSession, client_id: int) -> None:
    """客户不在就抛 422 那一族的异常。

    显式查一次，是为了把「客户不存在」和「账户重复」两种 `IntegrityError` 分开
    ——都靠数据库约束兜的话，拿到的只是一句外键冲突，得去解析驱动的异常结构才
    知道是哪种，而给客户端的提示天差地别。
    """
    found = await session.scalar(select(Client.id).where(Client.id == client_id))
    if found is None:
        raise ReferenceNotFoundError(f"客户不存在：{client_id}")


async def _flush_or_conflict(
    session: AsyncSession,
    *,
    platform: Platform,
    external_id: str,
) -> None:
    """把待写入刷进数据库，唯一冲突翻成领域异常。

    走到这里的 `IntegrityError` 只可能是 `(platform, external_id)` 撞了 ——
    外键那条已经被 `_ensure_client_exists` 提前挡掉。
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"{platform.value} 上已存在这个账户 ID：{external_id}") from exc


async def attach_credential(
    session: AsyncSession,
    account_id: int,
    *,
    credential_id: int | None,
) -> AdAccount:
    """把账户挂到某个平台凭据上；`None` 表示解绑。

    🔴 **挂上就是开自动拉取，解绑就是关。** 不另设 `auto_fetch` 开关的理由写在
    `models/ad_account.py` 的 `credential_id` 上。

    平台对不上抛 `ReferenceNotFoundError`（→ 422）。这个校验必须在这里做：数据库
    的外键只保证「那个凭据存在」，保证不了「它是同一个平台的」，而挂错的症状是
    每次拉取都被平台拒绝 —— 那个报错完全不提你挂错了。
    """
    account = await get(session, account_id)

    if credential_id is None:
        account.credential_id = None
        await session.flush()
        return account

    credential = await session.get(PlatformCredential, credential_id)
    if credential is None:
        raise ReferenceNotFoundError(f"平台凭据不存在：{credential_id}")
    if credential.platform is not account.platform:
        raise ReferenceNotFoundError(
            f"平台对不上：账户是 {account.platform.value}，凭据是 {credential.platform.value}"
        )

    account.credential_id = credential_id
    await session.flush()
    return account
