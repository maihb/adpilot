"""客户的业务逻辑。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models.client import Client
from adpilot.services.exceptions import ConflictError, NotFoundError

# 白名单挡的是「调用方拼错了字段名」：`update()` 收的是一个 Mapping，写错的键
# 会被 setattr 悄悄挂到实例上、一行也不改就返回成功。Celery 那边没有 Pydantic
# 兜底，所以这层检查不能省。
UPDATABLE_FIELDS = frozenset({"name", "note", "is_active"})


async def create(session: AsyncSession, *, name: str, note: str | None = None) -> Client:
    """建一个客户。重名抛 `ConflictError`。"""
    client = Client(name=name, note=note)
    session.add(client)
    await _flush_or_conflict(session, name=name)
    return client


async def get(session: AsyncSession, client_id: int) -> Client:
    """按 ID 取客户，不存在抛 `NotFoundError`。"""
    client = await session.get(Client, client_id)
    if client is None:
        raise NotFoundError(f"客户不存在：{client_id}")
    return client


async def list_page(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    is_active: bool | None = None,
) -> tuple[Sequence[Client], int]:
    """分页列出客户，返回 (本页, 过滤后总数)。

    排序固定按 `id` 倒序（新建的在前）。**offset 分页必须有确定的排序** ——
    没有 ORDER BY 时 PostgreSQL 不保证两次查询的行序一致，翻页会漏行或重行，
    而这种错在测试里几乎撞不到，只在数据多起来之后偶发。
    """
    filters = [] if is_active is None else [Client.is_active == is_active]

    total = await session.scalar(select(func.count(Client.id)).where(*filters))
    rows = await session.scalars(
        select(Client)
        .where(*filters)
        .order_by(Client.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def update(
    session: AsyncSession,
    client_id: int,
    changes: Mapping[str, object],
) -> Client:
    """按 `changes` 里出现的键更新客户；没出现的键保持不动。

    **收 Mapping 而不是一串 `X | None` 参数**，是为了区分「没传这个字段」和
    「把 `note` 显式清成 null」—— 后者是合法操作，而 `None` 没法同时表示两者。
    键名与 ORM 列名一致，`api/` 层用 `model_dump(exclude_unset=True)` 产出。
    """
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        # 编程错误，不是业务错误 —— 不该翻译成 4xx 回给客户端。
        raise ValueError(f"不可更新的字段：{sorted(unknown)}")

    client = await get(session, client_id)
    for field, value in changes.items():
        setattr(client, field, value)

    name = changes.get("name")
    await _flush_or_conflict(session, name=name if isinstance(name, str) else None)
    return client


async def _flush_or_conflict(session: AsyncSession, *, name: str | None) -> None:
    """把待写入刷进数据库，唯一冲突翻成领域异常。

    这张表上只有一条唯一约束（`name`），所以不必去解析约束名 —— 那要摸 asyncpg
    的异常内部结构，换个驱动就碎。**加第二条唯一约束时这里要跟着分辨**，否则
    错误信息会指向错的字段。
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"客户名已存在：{name}" if name else "客户已存在") from exc
