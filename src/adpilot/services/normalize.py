"""归一化：把 Mongo 里的原始快照变成 `daily_metrics`。

**一次单向转换。** 输入永远是快照（不是上传的文件），所以映射规则改了、或者发现
某个字段取错了，重跑一次就能修正历史 —— 这是养第二个库的全部理由。

两条规则决定了这一层的形状：

* **同一个 (账户, 层级, 日期) 取 `fetched_at` 最新的那条快照。** 平台数据在若干
  天内还会变（归因回传、无效流量剔除、汇率重算），重导会留下多条快照，最新的那条
  才是当前最准确的说法。旧的不删 —— 「当时那个数是多少」只能从它们查。
* **按唯一键 upsert，不 insert。** `(account_id, level, object_id, stat_date)`
  重复就更新。没有这个的话，同一天导两次就是双倍花费。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.db.mongo import RAW_REPORTS, MongoDatabase
from adpilot.models.ad_account import AdAccount
from adpilot.models.daily_metric import DailyMetric, MetricLevel
from adpilot.services import ad_account as ad_account_service
from adpilot.services import field_maps
from adpilot.services.exceptions import InvalidDataError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NormalizeSummary:
    account_id: int
    days: list[date]
    rows: int
    snapshots: int
    skipped_rows: int


async def normalize_account(
    session: AsyncSession,
    mongo: MongoDatabase,
    *,
    account_id: int,
    stat_date: date | None = None,
) -> NormalizeSummary:
    """把某账户的快照归一化进 `daily_metrics`。

    `stat_date` 给了就只跑那一天，不给就跑该账户的全部快照（重跑历史用）。
    账户不存在抛 `NotFoundError`，快照里缺必需列抛 `InvalidDataError`。
    """
    account = await ad_account_service.get(session, account_id)

    snapshots = await _latest_snapshots(mongo, account_id=account_id, stat_date=stat_date)
    if not snapshots:
        return NormalizeSummary(account_id=account_id, days=[], rows=0, snapshots=0, skipped_rows=0)

    values: list[dict[str, Any]] = []
    skipped = 0
    days: set[date] = set()
    for snapshot in snapshots:
        rows, snapshot_skipped = _rows_from_snapshot(snapshot, account=account)
        values.extend(rows)
        skipped += snapshot_skipped
        days.add(date.fromisoformat(str(snapshot["stat_date"])))

    if values:
        await _upsert(session, values)

    summary = NormalizeSummary(
        account_id=account_id,
        days=sorted(days),
        rows=len(values),
        snapshots=len(snapshots),
        skipped_rows=skipped,
    )
    log.info(
        "snapshots_normalized",
        account_id=account_id,
        days=len(summary.days),
        rows=summary.rows,
        snapshots=summary.snapshots,
        skipped_rows=summary.skipped_rows,
    )
    return summary


async def _latest_snapshots(
    mongo: MongoDatabase,
    *,
    account_id: int,
    stat_date: date | None,
) -> list[dict[str, Any]]:
    """每个 (层级, 日期) 只取 `fetched_at` 最新的那条快照。

    用聚合而不是「查回来在 Python 里挑」：重导多次的账户，快照数是天数的好几倍，
    把它们全拉回进程只为了扔掉大部分，纯属浪费。
    """
    match: dict[str, Any] = {"account_id": account_id}
    if stat_date is not None:
        match["stat_date"] = stat_date.isoformat()

    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {"$sort": {"fetched_at": -1}},
        {
            "$group": {
                "_id": {"level": "$level", "stat_date": "$stat_date"},
                "doc": {"$first": "$$ROOT"},
            }
        },
    ]
    cursor = mongo[RAW_REPORTS].aggregate(pipeline)
    return [group["doc"] async for group in cursor]


def _rows_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    account: AdAccount,
) -> tuple[list[dict[str, Any]], int]:
    """把一条快照的 payload 映射成 `daily_metrics` 的行。返回 (行, 跳过数)。"""
    level = MetricLevel(snapshot["level"])
    day = date.fromisoformat(str(snapshot["stat_date"]))
    payload: Sequence[Mapping[str, Any]] = snapshot.get("payload") or []
    if not payload:
        return [], 0

    columns = _resolve_columns(payload[0], level=level, day=day)

    rows: list[dict[str, Any]] = []
    skipped = 0
    for row in payload:
        object_id = str(row.get(columns["object_id"], "")).strip()
        if not object_id:
            # 没有对象 ID 的行进不了唯一键。这通常是导出文件里残留的小计行。
            skipped += 1
            continue

        rows.append(
            {
                "account_id": account.id,
                "level": level,
                "object_id": object_id,
                "object_name": _optional_text(row, columns.get("object_name")),
                "stat_date": day,
                # 币种取**账户**的，不从列名里解析。`Amount spent (USD)` 那个后缀
                # 是导出当时的账户币种，而这里要的是这一行数据该按什么币种解释 ——
                # 两者在正常情况下相同，不同的时候（账户改过币种）该信账户。
                "currency": account.currency,
                "spend": _number(row, columns.get("spend"), day),
                "impressions": _count(row, columns.get("impressions"), day),
                "clicks": _count(row, columns.get("clicks"), day),
                "conversions": _number(row, columns.get("conversions"), day),
                "revenue": _number(row, columns.get("revenue"), day),
                "reach": _optional_count(row, columns.get("reach"), day),
            }
        )
    return rows, skipped


def _resolve_columns(
    sample: Mapping[str, Any],
    *,
    level: MetricLevel,
    day: date,
) -> dict[str, str]:
    """按表头解析出各字段对应的列名。

    只看第一行 —— CSV 的表头对整份文件是一样的，逐行重算纯属浪费。找不到
    `object_id` 就直接报错：那一列是唯一键的一部分，缺了整份数据都进不去。
    """
    object_id = field_maps.find_column(sample, field_maps.OBJECT_ID_COLUMNS[level])
    if object_id is None:
        raise InvalidDataError(
            f"{day} 的 {level.value} 层级快照里找不到对象 ID 列。"
            f"试过 {list(field_maps.OBJECT_ID_COLUMNS[level])}，"
            f"实际表头是 {list(sample)}。"
            "层级填错是最常见的原因 —— 导出时选的是什么层级，导入时就填什么。"
        )

    resolved = {"object_id": object_id}
    optional = {
        "object_name": field_maps.OBJECT_NAME_COLUMNS[level],
        "spend": field_maps.SPEND_COLUMNS,
        "impressions": field_maps.IMPRESSIONS_COLUMNS,
        "clicks": field_maps.CLICKS_COLUMNS,
        "conversions": field_maps.CONVERSIONS_COLUMNS,
        "revenue": field_maps.REVENUE_COLUMNS,
        "reach": field_maps.REACH_COLUMNS,
    }
    for field, candidates in optional.items():
        found = field_maps.find_column(sample, candidates)
        if found is not None:
            resolved[field] = found
    return resolved


def _optional_text(row: Mapping[str, Any], column: str | None) -> str | None:
    if column is None:
        return None
    value = str(row.get(column, "")).strip()
    return value or None


def _number(row: Mapping[str, Any], column: str | None, day: date) -> Decimal:
    """金额与可能带小数的计数。列不存在时按 0 —— 平台不给这个字段是正常的。"""
    if column is None:
        return Decimal(0)
    try:
        return field_maps.parse_decimal(row.get(column))
    except ValueError as exc:
        raise InvalidDataError(f"{day} 的 {column!r} 列解析失败：{exc}") from exc


def _count(row: Mapping[str, Any], column: str | None, day: date) -> int:
    if column is None:
        return 0
    try:
        return field_maps.parse_int(row.get(column))
    except ValueError as exc:
        raise InvalidDataError(f"{day} 的 {column!r} 列解析失败：{exc}") from exc


def _optional_count(row: Mapping[str, Any], column: str | None, day: date) -> int | None:
    """reach 可空：平台不一定给这个字段，而 0 和「没这个数」在报表里不是一回事。"""
    if column is None:
        return None
    raw = str(row.get(column, "")).strip()
    return _count(row, column, day) if raw else None


async def _upsert(session: AsyncSession, values: list[dict[str, Any]]) -> None:
    """按唯一键 upsert。

    🔴 **`set_` 里必须显式带 `updated_at`。** `ON CONFLICT DO UPDATE` 走的是 Core
    语句、绕过 ORM，`TimestampMixin` 上那个 `onupdate=now()` 根本不会触发（那个
    类的 docstring 专门警告了这件事）。漏了的症状是「数据明明重导过、updated_at
    还停在上次」，而排查回填问题时看的正是这个列。
    """
    stmt = insert(DailyMetric).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["account_id", "level", "object_id", "stat_date"],
            set_={
                "object_name": stmt.excluded.object_name,
                "currency": stmt.excluded.currency,
                "spend": stmt.excluded.spend,
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "conversions": stmt.excluded.conversions,
                "revenue": stmt.excluded.revenue,
                "reach": stmt.excluded.reach,
                "updated_at": func.now(),
            },
        )
    )
