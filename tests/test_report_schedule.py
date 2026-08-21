"""定时日报：什么时候该出这一份。

判定（`_closed_days` / `due_reports`）是这一段唯一有推理的地方，而它踩的全是跨时区
那类**错了不报错**的坑 —— 所以这里的用例密度比编排那边高。

`_closed_days` 是纯函数（只读账户上的两个字段 + 一个时刻），所以大部分用例不用起
数据库。端到端那几条在 `test_reports_api.py`。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adpilot.config import Settings
from adpilot.db.postgres import create_engine, create_session_factory
from adpilot.models.ad_account import AdAccount, Platform
from adpilot.schemas.ad_account import MAX_REPORT_DELAY_HOURS
from adpilot.services import report as report_service
from adpilot.services.exceptions import InvalidDataError, QuotaExceededError


def _account(timezone: str, *, delay: int = 0) -> AdAccount:
    """一个只填了判定用得着那几个字段的账户。不进数据库。"""
    return AdAccount(
        id=1,
        client_id=1,
        platform=Platform.TIKTOK,
        external_id="demo-schedule",
        name="测试账户",
        currency="USD",
        timezone=timezone,
        is_active=True,
        auto_report=True,
        report_delay_hours=delay,
    )


def test_a_day_is_not_due_until_it_has_ended_in_the_account_timezone() -> None:
    """🔴 判据是**账户时区下**那天有没有结束，不是服务器这边的「今天减一」。

    这条用例是这一段的全部要害。UTC 08-21 06:00 这一刻：

    * 洛杉矶（UTC-7）那时是 08-20 23:00 —— **08-20 还没过完**，现在出日报会得到
      一份缺最后一小时的快照，而快照是固定的、不会自己补上；
    * 上海（UTC+8）那时是 08-21 14:00 —— 08-20 早就结束了，该出。

    拿服务器时区去截的话，两个账户会被当成同一天处理，而两边都不会报错。
    """
    moment = datetime(2026, 8, 21, 6, 0, tzinfo=UTC)

    la = report_service._closed_days(_account("America/Los_Angeles"), moment)
    shanghai = report_service._closed_days(_account("Asia/Shanghai"), moment)

    assert date(2026, 8, 20) not in la, "洛杉矶的 08-20 那时还没过完"
    assert date(2026, 8, 20) in shanghai, "上海的 08-20 那时早就结束了"


def test_the_delay_pushes_the_day_out() -> None:
    """`report_delay_hours` 往后推那个判定点。"""
    # 上海时间 08-21 01:00 —— 08-20 刚结束一小时。
    moment = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)

    assert date(2026, 8, 20) in report_service._closed_days(_account("Asia/Shanghai"), moment)
    assert date(2026, 8, 20) not in report_service._closed_days(
        _account("Asia/Shanghai", delay=2), moment
    )


def test_only_the_recent_window_is_considered() -> None:
    """🔴 只回头看 `RECENT_DAYS` 天。

    不卡窗口的话，运营补导一份跨 28 天的历史 CSV 会一口气炸出 28 份日报 —— 以及
    28 次 LLM 调用。那是真金白银，而且没有人会看三周前的日报。
    """
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    days = report_service._closed_days(_account("UTC"), moment)

    assert len(days) == report_service.RECENT_DAYS
    assert max(days) == date(2026, 8, 20), "最近的那天该是昨天"
    assert min(days) == date(2026, 8, 21 - report_service.RECENT_DAYS)


def test_days_come_back_oldest_first() -> None:
    """顺序是**旧的在前**。

    补数据时一次会有好几天该出，而按时间正序生成让日报的 `id` 顺序和日期顺序
    一致 —— 运营翻列表时不会看到「08-20 的比 08-19 的先生成」这种读不懂的排列。
    """
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    days = report_service._closed_days(_account("UTC"), moment)

    assert days == sorted(days)


def test_dst_day_is_measured_by_the_next_midnight_not_plus_24h() -> None:
    """🔴 夏令时切换那天**不是 24 小时**，所以判据是「次日零点」而不是「+24h」。

    美国 2026-11-01 回拨一小时，那天在洛杉矶是 **25 小时**。拿「那天零点 + 24
    小时」当结束点的话，会在真正的日切**前一小时**就认为它结束了 —— 于是那份日报
    少了一小时的数据，而且只有一年两次、只在那两天出现。

    这里取 11-01 23:30 PST（= 11-02 07:30 UTC）：按「+24h」算 11-01 早已结束，
    按真实的次日零点算还差半小时。
    """
    moment = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
    days = report_service._closed_days(_account("America/Los_Angeles"), moment)

    assert date(2026, 11, 1) not in days, "回拨日那天有 25 小时，此刻还没过完"


@pytest.mark.parametrize("timezone", ["America/Los_Angeles", "Asia/Shanghai", "UTC"])
def test_a_huge_delay_yields_nothing_rather_than_something_wrong(timezone: str) -> None:
    """延迟大到盖过整个回看窗口时，返回空 —— 而不是退回到某个「差不多」的日子。

    ⚠️ 这也是 `MAX_REPORT_DELAY_HOURS`（72）存在的理由：填一个更大的数会让那个
    账户**永远不出日报**，且没有任何东西会说为什么。上限把它挡在配置那一层。
    """
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    huge = report_service.RECENT_DAYS * 24 + 1

    assert report_service._closed_days(_account(timezone, delay=huge), moment) == []


# --- 判定接到真实数据上 ------------------------------------------------------
#
# 上面那几条把 `_closed_days` 的时区推理钉死了，这里验的是另外两个条件（那天有
# 没有数据、有没有已经生成过），以及两个账户级开关 —— 它们都要真的查库。

_TZ = "America/Los_Angeles"


async def _new_account(api: AsyncClient, suffix: str, **overrides: object) -> int:
    """建一个测试账户。

    ⚠️ **默认把 `report_delay_hours` 设成 0**，除非用例自己指定。

    生产默认是 2，而账户时区下的 00:00–02:00 之间「昨天」还没过延迟 —— 那是那个
    参数正确的行为，但会让下面这些用例在一天里的某两个小时集体失败。用 0 之后
    「账户时区下的昨天」就等价于「该出」，任何时刻都成立。
    """
    overrides.setdefault("report_delay_hours", 0)
    client = await api.post("/api/clients", json={"name": f"测试客户-排期-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "tiktok",
            "external_id": f"demo-schedule-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": _TZ,
            **overrides,
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


async def _import(api: AsyncClient, account_id: int, day: date) -> None:
    header = "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,Link clicks\n"
    body = f"{day.isoformat()},cmp-1,测试系列,120.00,1000,50\n"
    imported = await api.post(
        "/api/imports",
        files={"file": ("report.csv", (header + body).encode(), "text/csv")},
        data={"account_id": str(account_id), "level": "campaign"},
    )
    assert imported.status_code == 201, imported.text
    assert (await api.post(f"/api/ad-accounts/{account_id}/normalize")).status_code == 200


def _yesterday() -> date:
    """账户时区下的昨天 —— 一定落在回看窗口里，且一定已经结束。"""
    return datetime.now(ZoneInfo(_TZ)).date() - timedelta(days=1)


async def _due_for(session: AsyncSession, account_id: int) -> list[date]:
    due = await report_service.due_reports(session)
    return [item.stat_date for item in due if item.account_id == account_id]


@pytest.mark.integration
async def test_a_day_with_data_and_no_report_is_due(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    account_id = await _new_account(live_api, "该出")
    await _import(live_api, account_id, _yesterday())

    assert await _due_for(live_session, account_id) == [_yesterday()]


@pytest.mark.integration
async def test_a_day_without_data_is_never_due(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    """🔴 没有指标数据的那天不出日报。

    生成出来会是一份**空日报**，而空日报比没有日报更糟 —— 它看起来像是「昨天
    什么都没花」，而真相是「那天的数据还没导进来」。
    """
    account_id = await _new_account(live_api, "没数据")

    assert await _due_for(live_session, account_id) == []


@pytest.mark.integration
async def test_an_existing_draft_blocks_regeneration(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    """🔴 已经有日报（**哪怕只是草稿**）就不再生成。

    这是幂等的落点，也不只是幂等：人可能正在改那份 draft，而重新生成会把他改好
    的那版清掉（`generate` 的既有行为）。定时那条链绝不能替人做这个决定。
    """
    account_id = await _new_account(live_api, "已有草稿")
    day = _yesterday()
    await _import(live_api, account_id, day)

    assert await _due_for(live_session, account_id) == [day]

    generated = await live_api.post(
        f"/api/ad-accounts/{account_id}/reports", json={"stat_date": day.isoformat()}
    )
    assert generated.status_code == 201, generated.text
    assert generated.json()["status"] == "draft"

    assert await _due_for(live_session, account_id) == [], "已经有草稿了还判成该出"


@pytest.mark.integration
async def test_auto_report_off_skips_the_whole_account(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    """`auto_report=false` 的账户整个跳过 —— 那是省钱的闸门。"""
    account_id = await _new_account(live_api, "关掉自动", auto_report=False)
    await _import(live_api, account_id, _yesterday())

    assert await _due_for(live_session, account_id) == []


@pytest.mark.integration
async def test_paused_accounts_are_skipped(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    """停投的账户不看，同告警巡检。

    ⚠️ 这条和 `auto_report` 是**两个**开关：停投的账户仍然可能要日报（复盘那几天
    怎么停的），所以停用要显式做，而不是靠 `auto_report` 兼职。
    """
    account_id = await _new_account(live_api, "停投")
    await _import(live_api, account_id, _yesterday())
    assert (
        await live_api.patch(f"/api/ad-accounts/{account_id}", json={"is_active": False})
    ).status_code == 200

    assert await _due_for(live_session, account_id) == []


@pytest.mark.integration
async def test_the_delay_switch_takes_effect_end_to_end(
    live_api: AsyncClient,
    live_session: AsyncSession,
) -> None:
    """延迟拉满之后，昨天那份就不该出了。

    验的是那个配置真的**被读到了** —— 光测 `_closed_days` 只能证明函数对，证明
    不了这一列被接进了查询、也证明不了建账户时它没被默认值盖掉（那个 bug 真的
    发生过：schema 加了字段而 `services/ad_account.py` 没传，接口照常 201）。

    ⚠️ 断言的是「昨天不在里面」而不是「一份都不出」：`MAX_REPORT_DELAY_HOURS`
    是 72 小时，恰好等于回看窗口，所以窗口最早那一天可能仍然满足条件 —— 那不是
    bug，是两个参数边界相接。
    """
    account_id = await _new_account(live_api, "长延迟", report_delay_hours=MAX_REPORT_DELAY_HOURS)
    await _import(live_api, account_id, _yesterday())

    assert _yesterday() not in await _due_for(live_session, account_id)


# --- 编排的两条护栏 ----------------------------------------------------------
#
# `generate_due` 里真正有决定的只有两处：撞额度**中断整轮**、单份失败**继续走**。
#
# 🔴 **这两条把 `due_reports` 也一起换掉了**，理由是踩出来的：原先只换 `generate`、
# 让判定去读真实的库，结果那两条用例的成败取决于「此刻库里恰好有几份该出的日报」
# —— 在刚跑过一轮生成之后是 0 份，于是断言直接落空；CI 上的空库同理。
#
# 编排逻辑和库里有什么**本来就无关**，让它去读库只是把一个确定的测试变成了掷骰子。


def _due(count: int) -> list[report_service.DueReport]:
    return [
        report_service.DueReport(
            account_id=index + 1,
            account_name=f"测试账户-{index + 1}",
            stat_date=date(2026, 8, 18) + timedelta(days=index),
        )
        for index in range(count)
    ]


@pytest.mark.integration
async def test_quota_exhaustion_stops_the_whole_round(
    live_api: AsyncClient,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 撞上每日额度上限就**停下来**，不是跳过这一个继续。

    继续只会把剩下的调用全撞在同一堵墙上，然后日志里全是同一个错 —— 而真正要
    传达的信息（今天的额度没了）被淹在里面。已生成的那些留着，`quota_exhausted`
    如实报出去，否则「今天怎么少了几份」会变成一桩无头案。
    """
    calls: list[int] = []

    async def _boom(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise QuotaExceededError("今日 LLM 调用已达上限")

    async def _three(*args: object, **kwargs: object) -> list[report_service.DueReport]:
        return _due(3)

    monkeypatch.setattr(report_service, "due_reports", _three)
    monkeypatch.setattr(report_service, "generate", _boom)
    summary = await report_service.generate_due(_factory(live_settings), live_settings)

    assert summary.quota_exhausted is True
    assert summary.generated == 0
    assert len(calls) == 1, f"撞了额度还在继续试，一共调了 {len(calls)} 次（该只有 1 次）"


@pytest.mark.integration
async def test_one_bad_account_does_not_stop_the_others(
    live_api: AsyncClient,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一份出不来不拖累其余的 —— 计进 `failed`，接着走下一个。

    这条和上面那条刚好相反，而区别就在异常的种类：数据本身的问题是**局部**的
    （这个账户的时区名填错了），额度是**全局**的（后面每一个都会撞上）。
    """
    calls: list[int] = []

    async def _fail_first(*args: object, **kwargs: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise InvalidDataError("这个账户的时区名不存在")

    async def _three(*args: object, **kwargs: object) -> list[report_service.DueReport]:
        return _due(3)

    monkeypatch.setattr(report_service, "due_reports", _three)
    monkeypatch.setattr(report_service, "generate", _fail_first)
    summary = await report_service.generate_due(_factory(live_settings), live_settings)

    assert len(calls) == 3, "第一个失败之后就不走了 —— 一份出不来不该拖累其余的"
    assert summary.failed == 1
    assert summary.generated == 2
    assert summary.quota_exhausted is False


def _factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """一个真的 session 工厂。

    ⚠️ **不能用 `live_session` 那个夹具** —— `generate_due` 收的就是工厂，因为
    每份日报要各自一个事务（一份失败不能让前面已生成的一起回滚）。而上面两条
    用例把 `due_reports` 和 `generate` 都换掉了，所以这个工厂开出来的事务全是
    空的，一行都不会留下。
    """
    return create_session_factory(create_engine(settings))
