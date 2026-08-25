"""自动拉取的整条链：解密凭据 → 问 provider → 落快照 → 归一化 → 记结局。

**为什么这条必须是集成测试。** 单测能验每一段（provider 的错误分类、加解密、
停更判定），验不了它们接起来之后是不是同一批数据 —— 而这条链上有两个库
（快照在 Mongo、指标在 PostgreSQL）和一次跨库的形状转换。

用假 provider 跑，所以**不需要任何平台凭据**，这正是它在审核期间存在的理由
（docs/design/2026-08-25-ads-api-fetch.md 第十一节）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.auth import crypto
from adpilot.config import Settings
from adpilot.db.mongo import RAW_REPORTS, MongoDatabase
from adpilot.models.ad_account import AdAccount, Platform
from adpilot.models.balance import Balance
from adpilot.models.client import Client
from adpilot.models.daily_metric import DailyMetric, MetricLevel
from adpilot.models.fetch import FetchState, PlatformCredential
from adpilot.providers.base import AccountBalance, FetchError
from adpilot.providers.fake_api import FakeFetchProvider
from adpilot.services import credential as credential_service
from adpilot.services import fetch as fetch_service
from adpilot.services import normalize as normalize_service
from adpilot.services.exceptions import UpstreamError

pytestmark = pytest.mark.integration

#: 32 个字符，卡在 `auth/crypto.py` 的下限上。
SECRET = SecretStr("0123456789abcdef0123456789abcdef")


def _settings(live_settings: Settings) -> Settings:
    """给测试配上加密密钥。`Settings` 是 frozen 的，所以造一份副本。"""
    return live_settings.model_copy(update={"credentials_secret": SECRET})


async def _fixture_account(session: AsyncSession) -> AdAccount:
    """一个挂着假凭据的账户。**时区固定 UTC**，好让「昨天」这件事在断言里可算。"""
    # 名字带时间戳：`clients.name` 是唯一的，而这个夹具每个用例都要建一个。
    client = Client(name=f"示例｜自动拉取集成测试 {datetime.now(UTC).timestamp()}")
    session.add(client)
    await session.flush()

    credential = PlatformCredential(
        platform=Platform.TIKTOK,
        provider=FakeFetchProvider.name,
        label="示例｜集成测试授权",
        access_token=crypto.encrypt(SECRET, SecretStr("demo-not-a-real-token")),
        external_account_ids=[],
    )
    session.add(credential)
    await session.flush()

    account = AdAccount(
        client_id=client.id,
        platform=Platform.TIKTOK,
        external_id=f"demo-fetch-{datetime.now(UTC).timestamp()}",
        name="示例｜自动拉取账户",
        currency="USD",
        timezone="UTC",
        credential_id=credential.id,
        # 日切之后不等待，好让「最近一个已结束的日子」稳定是昨天。
        report_delay_hours=0,
    )
    session.add(account)
    await session.flush()
    return account


async def test_the_whole_chain_lands_metrics_and_a_balance(
    live_session: AsyncSession,
    live_mongo: MongoDatabase,
    live_settings: Settings,
) -> None:
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    summary = await fetch_service.fetch_account(
        live_session, live_mongo, settings, account_id=account.id
    )

    # 默认窗口是最近 3 天、且**不含今天** —— 当天数据没走完，落进去会让异动规则
    # 读出「花费暴跌」。
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    assert summary.until == yesterday
    assert summary.since == yesterday - timedelta(days=2)
    # 两个层级 × 三天 = 六条快照文档。
    assert summary.snapshots == len(fetch_service.DEFAULT_LEVELS) * 3
    assert summary.rows > 0
    assert summary.balance_captured is True

    stored = await live_mongo[RAW_REPORTS].count_documents({"account_id": account.id})
    assert stored == summary.snapshots

    # 快照落下去还不算数 —— 归一化读得懂它，才说明两边的形状是接上的。
    normalized = await normalize_service.normalize_account(
        live_session, live_mongo, account_id=account.id
    )
    assert normalized.rows > 0

    metric_days = await live_session.scalars(
        select(DailyMetric.stat_date).where(DailyMetric.account_id == account.id).distinct()
    )
    assert sorted(metric_days.all()) == [
        summary.since + timedelta(days=offset) for offset in range(3)
    ]

    levels = await live_session.scalars(
        select(DailyMetric.level).where(DailyMetric.account_id == account.id).distinct()
    )
    assert set(levels.all()) == {MetricLevel.ACCOUNT, MetricLevel.CAMPAIGN}

    balance = await live_session.scalar(
        select(Balance).where(Balance.account_id == account.id).order_by(Balance.id.desc())
    )
    assert balance is not None
    assert balance.note == fetch_service.BALANCE_NOTE
    # 币种取账户的，不取 provider 报的 —— 余额和消耗必须同币种才能相除。
    assert balance.currency == "USD"


async def test_pulling_twice_does_not_double_the_spend(
    live_session: AsyncSession,
    live_mongo: MongoDatabase,
    live_settings: Settings,
) -> None:
    """🔴 重复拉取必须幂等 —— 这是敢开滚动窗口（每天拉最近 3 天）的全部前提。

    快照会多一份（append-only，那是刻意的），但 `daily_metrics` 按唯一键 upsert，
    行数和数字都不变。
    """
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    await fetch_service.fetch_account(live_session, live_mongo, settings, account_id=account.id)
    await normalize_service.normalize_account(live_session, live_mongo, account_id=account.id)

    first_rows = await live_session.scalar(
        select(func.count()).select_from(DailyMetric).where(DailyMetric.account_id == account.id)
    )
    first_spend = await live_session.scalar(
        select(func.sum(DailyMetric.spend)).where(DailyMetric.account_id == account.id)
    )

    await fetch_service.fetch_account(live_session, live_mongo, settings, account_id=account.id)
    await normalize_service.normalize_account(live_session, live_mongo, account_id=account.id)

    second_rows = await live_session.scalar(
        select(func.count()).select_from(DailyMetric).where(DailyMetric.account_id == account.id)
    )
    second_spend = await live_session.scalar(
        select(func.sum(DailyMetric.spend)).where(DailyMetric.account_id == account.id)
    )

    assert second_rows == first_rows
    assert second_spend == first_spend

    # 快照那边**应该**变多：「当时这个数是多少」只能从它们查。
    snapshots = await live_mongo[RAW_REPORTS].count_documents({"account_id": account.id})
    assert snapshots == len(fetch_service.DEFAULT_LEVELS) * 3 * 2


async def test_a_failure_is_recorded_so_the_sweep_can_see_it(
    live_session: AsyncSession,
    live_mongo: MongoDatabase,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 拉取失败要留下痕迹，否则「拉不到数」永远没人知道。

    这条盯的是设计文档第三节那个最危险的失败模式：拉取停了，看板显示花费 0，
    而 0 花费和「昨天没投放」长得一模一样。`fetch_states` 是唯一能把两者分开的
    地方，而告警巡检读的正是它。
    """
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    monkeypatch.setattr(
        credential_service,
        "open_provider",
        lambda _settings, _credential: FakeFetchProvider(fail=True),
    )

    with pytest.raises(UpstreamError) as exc_info:
        await fetch_service.fetch_account(live_session, live_mongo, settings, account_id=account.id)
    assert exc_info.value.retryable is False

    # 真实的调用方（`tasks/fetch.py`）会在**另一个事务**里记这一笔 —— 这里直接调
    # 那个函数，验的是计数和错误消息真的落了下来。
    await fetch_service.record_failure(
        live_session, account_id=account.id, error=exc_info.value.message
    )
    await fetch_service.record_failure(
        live_session, account_id=account.id, error=exc_info.value.message
    )

    state = await live_session.get(FetchState, account.id)
    assert state is not None
    # 计数在数据库里自增，不是读出来加一 —— 后者在并发下会把两次失败记成一次。
    assert state.consecutive_failures == 2
    assert state.last_error
    assert state.last_success_at is None


async def test_a_success_clears_the_previous_error(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """成功之后要把错误消息清掉。

    留着一条已经修好的报错，只会让下一个来看的人以为现在还坏着。
    """
    account = await _fixture_account(live_session)

    await fetch_service.record_failure(live_session, account_id=account.id, error="token 失效")
    await fetch_service.record_success(live_session, account_id=account.id)

    state = await live_session.get(FetchState, account.id)
    assert state is not None
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.last_success_at is not None


async def test_due_accounts_skips_what_was_already_pulled_today(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """扫描的判据是「这个日切之后成功拉过没有」，不是「几点了」。

    没有这一条的话，每小时的排期会对每个账户各发一次 API 请求 —— 一天 24 次，
    而其中 23 次拿回的是同一批数据。
    """
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    due = await fetch_service.due_accounts(live_session, settings)
    assert account.id in [item.id for item in due]

    await fetch_service.record_success(live_session, account_id=account.id)

    due_again = await fetch_service.due_accounts(live_session, settings)
    assert account.id not in [item.id for item in due_again]


async def test_an_account_without_a_credential_is_never_scanned(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """没挂凭据 = 不自动拉取。**这就是那个开关**，没有第二个。"""
    account = await _fixture_account(live_session)
    account.credential_id = None
    await live_session.flush()

    due = await fetch_service.due_accounts(live_session, _settings(live_settings))

    assert account.id not in [item.id for item in due]


async def test_a_credential_encrypted_with_another_secret_fails_loudly(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """`CREDENTIALS_SECRET` 换过之后要**大声失败**，不能拿着空 token 去请求平台。

    后者会得到一个和「密钥换过了」毫无关系的平台报错，而人会去查平台。
    """
    account = await _fixture_account(live_session)
    credential = await credential_service.get(live_session, account.credential_id or 0)
    wrong = live_settings.model_copy(
        update={"credentials_secret": SecretStr("ffffffffffffffffffffffffffffffff")}
    )

    with pytest.raises(Exception, match="重新授权"):
        credential_service.open_provider(wrong, credential)


async def test_fake_provider_is_refused_in_production(live_settings: Settings) -> None:
    """假 provider 在生产环境**造不出来**。

    示例数据落进生产库不会报错，只会让看板上多出几行 `demo-` 开头的花费，而它们
    会被一起算进汇总。同 `seed.py` 拒绝在生产执行。
    """
    from adpilot.config import Environment
    from adpilot.providers import registry
    from adpilot.providers.base import ParseError

    options = registry.FetchOptions(access_token=SecretStr("x"))
    production = live_settings.model_copy(update={"environment": Environment.PROD})

    with pytest.raises(ParseError, match="非生产"):
        registry.create_fetch(
            FakeFetchProvider.name,
            options,
            allow_fake=production.environment is not Environment.PROD,
        )


async def test_the_window_never_includes_today(
    live_session: AsyncSession,
    live_mongo: MongoDatabase,
    live_settings: Settings,
) -> None:
    """显式给了区间就照办（补历史用），不给就永远停在昨天。"""
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    explicit = await fetch_service.fetch_account(
        live_session,
        live_mongo,
        settings,
        account_id=account.id,
        since=date(2026, 1, 1),
        until=date(2026, 1, 2),
    )

    assert explicit.since == date(2026, 1, 1)
    assert explicit.until == date(2026, 1, 2)

    default = await fetch_service.fetch_account(
        live_session, live_mongo, settings, account_id=account.id
    )

    assert default.until < datetime.now(UTC).date()


async def test_balance_failure_does_not_fail_the_whole_pull(
    live_session: AsyncSession,
    live_mongo: MongoDatabase,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """余额拿不到不算故障 —— 有些账户类型平台根本不给余额，而日指标照样是好的。"""
    account = await _fixture_account(live_session)
    settings = _settings(live_settings)

    class _NoBalance(FakeFetchProvider):
        async def fetch_balance(self, *, external_id: str) -> AccountBalance:
            raise FetchError("这个账户没有余额", retryable=False)

    monkeypatch.setattr(
        credential_service, "open_provider", lambda _settings, _credential: _NoBalance()
    )

    summary = await fetch_service.fetch_account(
        live_session, live_mongo, settings, account_id=account.id
    )

    assert summary.balance_captured is False
    assert summary.rows > 0
