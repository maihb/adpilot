"""模型与数据库之间那条链路的集成测试。

验的不是「SQLAlchemy 会不会用」，而是三件**只有真连上 PostgreSQL 才知道**的事：

1. `Decimal` ↔ `numeric(20,4)` 往返不丢精度，以及超出精度时到底发生了什么；
2. `daily_metrics` 的唯一键真能挡住重复导入 —— 挡不住的话，同一天导两次就是
   双倍花费；
3. `StrEnumType` 存进去是 varchar、读回来是枚举成员。

全部要求 `RUN_INTEGRATION=1` 且表已经迁移过（`make migrate`）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models import AdAccount, Client, DailyMetric, MetricLevel, Platform

pytestmark = pytest.mark.integration


async def _account(session: AsyncSession, external_id: str) -> AdAccount:
    """造一个客户 + 广告账户，返回账户。"""
    client = Client(name=f"测试客户-{external_id}")
    session.add(client)
    await session.flush()

    account = AdAccount(
        client_id=client.id,
        platform=Platform.TIKTOK,
        external_id=external_id,
        name=f"测试账户-{external_id}",
        currency="USD",
        # 真实案例里的日切点差异就来自这种时区，不是随手挑的
        timezone="America/Anchorage",
    )
    session.add(account)
    await session.flush()
    return account


def _metrics_of(account: AdAccount) -> Select[tuple[DailyMetric]]:
    """只查这个账户的行。

    **别写成裸的 `select(DailyMetric)`。** 用例之间靠外层事务回滚隔离，但开发机上
    那个库里往往躺着手工试出来的数据 —— 不带条件的话 `scalar_one()` 会撞上
    `MultipleResultsFound`，而报错跟这条用例要验的事情毫无关系。CI 的库是空的，
    所以这种失败只在别人的机器上出现。
    """
    return select(DailyMetric).where(DailyMetric.account_id == account.id)


def _metric(account_id: int, **overrides: object) -> DailyMetric:
    defaults: dict[str, object] = {
        "account_id": account_id,
        "level": MetricLevel.CAMPAIGN,
        "object_id": "cmp-1",
        "stat_date": date(2026, 8, 18),
        "currency": "USD",
        "spend": Decimal("0"),
        "impressions": 0,
        "clicks": 0,
        "conversions": Decimal("0"),
        "revenue": Decimal("0"),
    }
    return DailyMetric(**(defaults | overrides))


async def test_money_survives_the_round_trip(live_session: AsyncSession) -> None:
    """金额写进去再读回来必须一模一样，且仍然是 Decimal。

    这条测的是「`Decimal` + `numeric` 这条链路不丢精度」。用 float 中转的话，
    1234.5678 这种值会在某一位上开始漂 —— 而广告花费错一分钱，这个系统输出的
    所有数字就都不可信了。
    """
    account = await _account(live_session, "acct-money")
    live_session.add(_metric(account.id, spend=Decimal("1234.5678"), revenue=Decimal("98765.4321")))
    await live_session.flush()

    # 必须把 identity map 清掉再查，否则读到的是内存里那个对象，根本没经过库
    live_session.expunge_all()
    stored = (await live_session.execute(_metrics_of(account))).scalar_one()

    assert stored.spend == Decimal("1234.5678")
    assert stored.revenue == Decimal("98765.4321")
    assert isinstance(stored.spend, Decimal)


async def test_excess_precision_is_rounded_not_rejected(live_session: AsyncSession) -> None:
    """超出 numeric(20,4) 精度的写入会被**四舍五入**，既不报错也不截断。

    把它钉成一条测试，是因为这个行为看不见：ROAS、CPA 这类除法结果会乘出很多位
    小数，落库时悄悄变了值，而没有任何报错提示你发生过这件事。
    """
    account = await _account(live_session, "acct-round")
    live_session.add(_metric(account.id, spend=Decimal("10.00005")))
    await live_session.flush()

    live_session.expunge_all()
    stored = (await live_session.execute(_metrics_of(account))).scalar_one()

    assert stored.spend == Decimal("10.0001")  # 截断的话会是 10.0000


async def test_same_day_reimport_is_blocked_by_the_unique_key(live_session: AsyncSession) -> None:
    """同一个 (账户, 层级, 对象, 日期) 插第二次必须失败。

    平台数据在若干天内还会变，所以重导是常态。归一化按这条唯一键 upsert；
    **唯一键没了，重导就是把同一天的花费再加一遍。**
    """
    account = await _account(live_session, "acct-dup")
    live_session.add(_metric(account.id, spend=Decimal("100")))
    await live_session.flush()

    live_session.add(_metric(account.id, spend=Decimal("200")))
    with pytest.raises(IntegrityError):
        await live_session.flush()


async def test_level_differs_from_object_id_in_the_unique_key(live_session: AsyncSession) -> None:
    """层级不同就是不同的行 —— 账户级和广告系列级的同一天数据要能共存。

    否则「账户当天花了 100」会和「某个系列当天花了 100」互相顶掉一个。
    """
    account = await _account(live_session, "acct-levels")
    live_session.add(_metric(account.id, level=MetricLevel.ACCOUNT, object_id="acct-levels"))
    live_session.add(_metric(account.id, level=MetricLevel.CAMPAIGN, object_id="cmp-1"))
    await live_session.flush()

    rows = (await live_session.execute(_metrics_of(account))).scalars().all()
    assert len(rows) == 2


async def test_enum_columns_read_back_as_enum_members(live_session: AsyncSession) -> None:
    """枚举列存的是 varchar，读回来必须是枚举成员而不是裸字符串。

    读回裸 str 的症状很隐蔽：`level is MetricLevel.ADGROUP` 这种判断会永远
    不成立，而 `==` 又照常为真（StrEnum 就是 str），于是错得很安静。
    """
    account = await _account(live_session, "acct-enum")
    live_session.add(_metric(account.id, level=MetricLevel.ADGROUP))
    await live_session.flush()

    live_session.expunge_all()
    stored = (await live_session.execute(_metrics_of(account))).scalar_one()
    account_back = (
        await live_session.execute(select(AdAccount).where(AdAccount.id == account.id))
    ).scalar_one()

    assert stored.level is MetricLevel.ADGROUP
    assert account_back.platform is Platform.TIKTOK
