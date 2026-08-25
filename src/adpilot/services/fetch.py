"""自动拉取：从平台 API 取数，落成快照，顺手记下这次的结局。

**和文件导入落到同一个地方、走同一条归一化**（`services/imports.py` 那份 docstring
讲的双库边界，这里一字不改地适用）：快照 append-only，字段一个都不映射，去重靠
归一化按唯一键 upsert。区别只有数据是**问来的**而不是**传来的**。

## 🔴 这一层不管事务，也不重试

两件事都交给调用方，因为它们在两条路径上的正确做法**相反**：

* **失败记录必须写在另一个事务里。** 拉取失败会让外层事务回滚 —— 连同刚写进
  `fetch_states` 的那条失败记录一起。于是「这个账户拉不到数」永远不会被巡检看见，
  而看板上那片 0 花费看起来一切正常。这个坑不会有任何报错，所以 `record_failure`
  是一个**独立的函数**、由调用方在自己的事务里调，而不是藏在这里的 except 分支中。
* **重试策略归 `tasks/`。** 同一个函数既被 HTTP 请求调用（人在等，要当场说），
  也被排期任务调用（没人等，该退避重试）。判据（`UpstreamError.retryable`）原样
  从 provider 带上来，怎么用由上面决定。

设计见[自动拉取平台数据](../../../docs/design/2026-08-25-ads-api-fetch.md)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings
from adpilot.db.mongo import RAW_REPORTS, MongoDatabase
from adpilot.models.ad_account import AdAccount
from adpilot.models.daily_metric import MetricLevel
from adpilot.models.fetch import FetchState, PlatformCredential
from adpilot.providers.base import FetchError, FetchProvider, ParseResult
from adpilot.services import ad_account as ad_account_service
from adpilot.services import balance as balance_service
from adpilot.services import credential as credential_service
from adpilot.services.exceptions import ConflictError, InvalidDataError, UpstreamError

log = structlog.get_logger(__name__)

#: 默认拉哪几层。
#:
#: **账户级是权威汇总**（`services/daily_metric.py` 的 `LEVEL_PRIORITY` 首选它），
#: 系列级支撑日报里「哪个系列在跑」。**广告级刻意不拉**：行数是系列级的几十倍，
#: 而现在没有任何一屏用得上它 —— 真要看单条广告的表现时再加，那时它是这个元组
#: 里多一个成员的事。
DEFAULT_LEVELS: Final = (MetricLevel.ACCOUNT, MetricLevel.CAMPAIGN)

#: 余额快照的来源标记，写进 `balances.note`。
#:
#: **不是装饰**：`captured_at` 只能填拉取时刻（平台不告诉我们余额是什么时候的），
#: 而人在核对「充值前还是充值后」时必须知道这个时刻是怎么来的。
BALANCE_NOTE: Final = "自动拉取"

#: 往回找「已经结束的那天」时最多找几天。防的是账户时区配错（比如填了一个偏移
#: 极大的时区）导致的死循环 —— 正常情况下第一次或第二次就命中。
_MAX_LOOKBACK: Final = 7


@dataclass(frozen=True, slots=True)
class FetchSummary:
    """一次拉取的结果。**不含数据本身** —— 那可能是几千行。"""

    account_id: int
    provider: str
    since: date
    until: date
    levels: list[MetricLevel]

    #: 落了几条快照文档（= 天数 × 层数，其中有数据的那些）。
    snapshots: int
    rows: int

    #: 余额拉到没有。**`False` 不是失败** —— 有些账户类型（代理商代付）平台不给
    #: 余额，那时日指标照样是好的。
    balance_captured: bool


async def fetch_account(
    session: AsyncSession,
    mongo: MongoDatabase,
    settings: Settings,
    *,
    account_id: int,
    since: date | None = None,
    until: date | None = None,
    levels: tuple[MetricLevel, ...] = DEFAULT_LEVELS,
    now: datetime | None = None,
) -> FetchSummary:
    """拉一个账户的日指标与余额，落成快照。**不做归一化**。

    归一化留给调用方，因为两条路径的正确做法不同：接口那边排队（人不该对着转圈
    等几千行 upsert 完），任务那边直接同步跑（已经在 worker 里了，再投一条消息
    只是让「拉完了但数字还没变」多出一个中间状态）。

    账户不存在抛 `NotFoundError`；没挂凭据抛 `InvalidDataError`；平台那边出问题
    抛 `UpstreamError`（带着 `retryable`）。
    """
    now = now or datetime.now(UTC)
    account = await ad_account_service.get(session, account_id)
    credential = await _require_credential(session, account)
    provider = credential_service.open_provider(settings, credential)

    window_since, window_until = _resolve_window(
        account, settings, since=since, until=until, now=now
    )

    documents: list[dict[str, Any]] = []
    rows = 0
    for level in levels:
        result = await _fetch_level(
            provider,
            external_id=account.external_id,
            level=level,
            since=window_since,
            until=window_until,
        )
        fetched_at = datetime.now(UTC)
        for day in result.days:
            rows += len(day.rows)
            documents.append(
                {
                    # 与文件导入的快照**同构**（`services/imports.py`）—— 归一化
                    # 读的是同一批键，它压根不需要知道这份数据是问来的还是传来的。
                    "provider": provider.name,
                    "account_id": account_id,
                    "level": level.value,
                    "stat_date": day.stat_date.isoformat(),
                    "fetched_at": fetched_at,
                    "payload": day.rows,
                    # 拉取特有：**当时问了平台什么**。排查「这天怎么少了一半行」
                    # 时，第一个要回答的就是「当时请求的区间对不对」，而那件事
                    # 从 payload 里推不出来。
                    "fetch": {
                        "since": window_since.isoformat(),
                        "until": window_until.isoformat(),
                        "external_id": account.external_id,
                    },
                }
            )

    if documents:
        await mongo[RAW_REPORTS].insert_many(documents)

    balance_captured = await _capture_balance(session, provider, account=account)

    summary = FetchSummary(
        account_id=account_id,
        provider=provider.name,
        since=window_since,
        until=window_until,
        levels=list(levels),
        snapshots=len(documents),
        rows=rows,
        balance_captured=balance_captured,
    )
    log.info(
        "platform_data_fetched",
        account_id=account_id,
        provider=provider.name,
        since=window_since.isoformat(),
        until=window_until.isoformat(),
        snapshots=summary.snapshots,
        rows=summary.rows,
        balance_captured=balance_captured,
    )
    return summary


async def due_accounts(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[AdAccount]:
    """扫出此刻**该拉但还没拉**的账户。

    判据和定时日报同一个形状 —— 「数据到齐了吗」而不是「几点了」：

    1. 账户在投（`is_active`）且挂着一个**在用**的凭据；
    2. 最近一个已结束的日子（账户时区日切 + `report_delay_hours`）已经到点；
    3. 那之后**还没有成功拉过** —— 拉过就跳过，于是每个账户每天大约拉一次，而
       不是每小时一次。

    ⚠️ 第 3 条让滚动窗口天然成立：每天拉一次、每次拉最近 N 天，于是同一天会被
    拉 N 次（当天一次，之后 N-1 天各一次）—— 平台后来回填的修正就是这样进来的。

    **本函数不写任何东西**，可以反复调用，也可以单独测。
    """
    now = now or datetime.now(UTC)

    accounts = (
        await session.scalars(
            select(AdAccount)
            .join(PlatformCredential, AdAccount.credential_id == PlatformCredential.id)
            .where(
                AdAccount.is_active.is_(True),
                PlatformCredential.is_active.is_(True),
            )
        )
    ).all()

    states = await _states_by_account(session, [account.id for account in accounts])

    due: list[AdAccount] = []
    for account in accounts:
        closed_at = _last_closed_cutover(account, now=now)
        if closed_at is None:
            continue
        state = states.get(account.id)
        if (
            state is not None
            and state.last_success_at is not None
            and state.last_success_at >= closed_at
        ):
            continue
        due.append(account)
    return due


async def record_success(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime | None = None,
) -> None:
    """记一次成功：清空错误、计数归零。

    **错误消息一定要清掉** —— 留着一条已经修好的报错，只会让下一个来看的人以为
    现在还坏着。
    """
    now = now or datetime.now(UTC)
    await _upsert_state(
        session,
        account_id=account_id,
        values={
            "last_attempt_at": now,
            "last_success_at": now,
            "last_error": None,
            "consecutive_failures": 0,
        },
    )


async def record_failure(
    session: AsyncSession,
    *,
    account_id: int,
    error: str,
    now: datetime | None = None,
) -> None:
    """记一次失败：计数 +1，留下原因。

    🔴 **必须在一个独立的事务里调用。** 拉取失败会让外层事务回滚，把这条记录
    一起带走 —— 而那正是这条记录唯一有用的时刻。模块 docstring 讲了为什么这件事
    没有任何报错。

    `error` 会原样进库、并出现在告警里，所以写**人能照着动手**的话（「token 失效，
    需要重新授权」），不要写驱动异常的原文（那里面可能带着 URL 和账户 ID）。
    """
    now = now or datetime.now(UTC)
    await _upsert_state(
        session,
        account_id=account_id,
        values={
            "last_attempt_at": now,
            # 截断到列宽以内。**报错文本比列宽长是常态**（平台的错误消息能很长），
            # 而一次写入失败会把「记下这次失败」这件事本身也搞砸。
            "last_error": error[:512],
        },
        increment_failures=True,
    )


async def state_for(session: AsyncSession, account_id: int) -> FetchState | None:
    """某个账户的拉取状态。没有行 = **从来没拉过**，不是「一切正常」。"""
    return await session.get(FetchState, account_id)


async def states_for(session: AsyncSession, account_ids: list[int]) -> dict[int, FetchState]:
    return await _states_by_account(session, account_ids)


async def _fetch_level(
    provider: FetchProvider,
    *,
    external_id: str,
    level: MetricLevel,
    since: date,
    until: date,
) -> ParseResult:
    try:
        return await provider.fetch(
            external_id=external_id,
            # provider 收的是字符串：那一层够不着 `MetricLevel`（分层契约），
            # 翻译成平台方言是它自己的事。
            level=level.value,
            since=since,
            until=until,
        )
    except FetchError as exc:
        raise UpstreamError(exc.message, retryable=exc.retryable) from exc


async def _capture_balance(
    session: AsyncSession,
    provider: FetchProvider,
    *,
    account: AdAccount,
) -> bool:
    """拉一条余额快照。

    ⚠️ **余额失败不让整次拉取失败。** 日指标已经落进 Mongo 了，为了一条余额把
    整次拉取标成失败，会让人以为连数据都没拉到 —— 而余额拿不到的常见原因（代理商
    代付的账户平台不给余额）根本不是故障。

    重复的 `captured_at` 直接跳过：手动触发和排期在同一秒撞上时会有这种事，而
    两条一模一样的余额快照不会让任何计算出错，只会让人在核对时怀疑自己看错了。
    """
    try:
        snapshot = await provider.fetch_balance(external_id=account.external_id)
    except FetchError as exc:
        log.warning(
            "balance_fetch_failed",
            account_id=account.id,
            retryable=exc.retryable,
            reason=exc.message,
        )
        return False

    try:
        await balance_service.record(
            session,
            account_id=account.id,
            available=snapshot.available,
            captured_at=snapshot.captured_at,
            note=BALANCE_NOTE,
        )
    except ConflictError:
        log.info("balance_snapshot_duplicate", account_id=account.id)
        return False
    return True


async def _require_credential(session: AsyncSession, account: AdAccount) -> PlatformCredential:
    if account.credential_id is None:
        raise InvalidDataError(
            f"账户 {account.id} 没有挂平台凭据，自动拉取无从谈起（数据仍可用 CSV 导入）"
        )
    credential = await credential_service.get(session, account.credential_id)
    if credential.platform is not account.platform:
        # 挂错凭据的症状是每次拉取都被平台拒绝，而那个报错完全不提「你挂错了」。
        raise InvalidDataError(
            f"账户 {account.id} 是 {account.platform.value}，"
            f"挂的凭据却是 {credential.platform.value}"
        )
    return credential


def _resolve_window(
    account: AdAccount,
    settings: Settings,
    *,
    since: date | None,
    until: date | None,
    now: datetime,
) -> tuple[date, date]:
    """算出要拉哪一段。两端都给了就照办（手动补历史用）。

    🔴 **默认不拉「今天」。** 当天的数据在账户时区下还没走完，落进 `daily_metrics`
    的是半天的数字 —— 而异动规则拿它去和历史基线比，会得出「花费暴跌」这种结论。
    真要看当天的实时消耗，那是余额那条线的事（它拉的就是此刻的状态）。
    """
    if since is not None and until is not None:
        if since > until:
            raise InvalidDataError(f"日期区间反了：{since.isoformat()} 晚于 {until.isoformat()}")
        return since, until

    resolved_until = until or _last_closed_day(account, now=now)
    resolved_since = since or resolved_until - timedelta(
        days=max(settings.fetch_window_days, 1) - 1
    )
    if resolved_since > resolved_until:
        raise InvalidDataError(
            f"日期区间反了：{resolved_since.isoformat()} 晚于 {resolved_until.isoformat()}"
        )
    return resolved_since, resolved_until


def _last_closed_day(account: AdAccount, *, now: datetime) -> date:
    """最近一个「已经结束且过了 `report_delay_hours`」的自然日。

    找不到就退回账户时区的昨天 —— 那只会发生在时区配得离谱的时候，而一个稍旧的
    区间远好过一次异常：拉取本来就是幂等的，多拉一天不会有任何后果。
    """
    tz = ZoneInfo(account.timezone)
    cutover = _last_closed_cutover(account, now=now)
    if cutover is None:
        return now.astimezone(tz).date() - timedelta(days=1)
    # 日切时刻是**那天结束**的瞬间，也就是次日零点 —— 所以那天本身要减一。
    # 直接拿 `cutover.date()` 会得到次日，于是每次都多拉一天还未结束的数据。
    return cutover.astimezone(tz).date() - timedelta(days=1)


def _last_closed_cutover(account: AdAccount, *, now: datetime) -> datetime | None:
    """最近一个已过 `report_delay_hours` 的日切时刻（UTC aware）。

    形状照搬定时日报的 `_closed_days`（`services/report.py`）：`datetime.combine`
    要带 `tzinfo` 才是那个时区的零点，换算成 UTC 之后才谈得上和 `now` 比大小。
    两处刻意各写一份而不是抽公共函数 —— 它们回答的问题不同（那边要「哪几天该出
    日报」，这边要「拉到哪天为止」），合并会让参数表长出一个「模式」开关。
    """
    tz = ZoneInfo(account.timezone)
    today = now.astimezone(tz).date()
    delay = timedelta(hours=account.report_delay_hours)

    for offset in range(1, _MAX_LOOKBACK + 1):
        day = today - timedelta(days=offset)
        closed_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
        if closed_at + delay <= now:
            return closed_at.astimezone(UTC)
    return None


async def _states_by_account(
    session: AsyncSession, account_ids: list[int]
) -> dict[int, FetchState]:
    if not account_ids:
        return {}
    rows = await session.scalars(select(FetchState).where(FetchState.account_id.in_(account_ids)))
    return {state.account_id: state for state in rows}


async def _upsert_state(
    session: AsyncSession,
    *,
    account_id: int,
    values: dict[str, Any],
    increment_failures: bool = False,
) -> None:
    """一账户一行，有则更新无则插入。

    用 `ON CONFLICT` 而不是「先查再写」：拉取任务和手动触发可能同时落到同一个
    账户上，而先查再写在那一刻会插入两行、撞主键、然后让整次拉取以一个和拉取
    毫无关系的报错失败。
    """
    payload = {"account_id": account_id, **values}
    if increment_failures:
        payload["consecutive_failures"] = 1

    statement = insert(FetchState).values(**payload)
    update: dict[str, Any] = dict(values)
    if increment_failures:
        # 计数在数据库里自增，不在 Python 里读出来加一 —— 后者在并发下会把两次
        # 失败记成一次。
        update["consecutive_failures"] = FetchState.consecutive_failures + 1
    await session.execute(
        statement.on_conflict_do_update(index_elements=[FetchState.account_id], set_=update)
    )
