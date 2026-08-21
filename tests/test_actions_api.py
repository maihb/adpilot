"""投放操作记录。

离线那几条验的是**不必起数据库就能验完**的两件事：接口契约，以及日切换算那个纯
函数 —— 后者是这个领域里唯一算错了也不报错的地方（一条记录悄悄归到相邻那一天，
于是日报里「今天做了什么」写的是昨天的事）。

集成那几条验的是「登记 → 查回来」这条链，以及日报真正要用的那个查询形态
（某个账户时区自然日区间里发生的事，按发生先后排）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.models import AdAccount, Client, Platform
from adpilot.models.action import ActionKind
from adpilot.services import action as action_service
from adpilot.services.action import window_bounds

# 与余额那套用同一个时区：真实案例里的日切点差异就来自这种时区，不是随手挑的。
_TZ = "America/Anchorage"


async def _account_row(session: AsyncSession, suffix: str) -> AdAccount:
    """直接落库造一个账户，供那几条不经 HTTP 的服务层用例使用。"""
    client = Client(name=f"测试客户-操作-{suffix}")
    session.add(client)
    await session.flush()

    account = AdAccount(
        client_id=client.id,
        platform=Platform.META,
        external_id=f"acct-action-{suffix}",
        name=f"测试账户-{suffix}",
        currency="USD",
        timezone=_TZ,
    )
    session.add(account)
    await session.flush()
    return account


async def _new_account(api: AsyncClient, suffix: str) -> int:
    client = await api.post("/api/clients", json={"name": f"测试客户-操作-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "meta",
            "external_id": f"acct-action-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": _TZ,
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "budget",
        "summary": "A 系列日预算 500 → 800",
        "reason": "周末 CPM 普涨，先扛量到周一再看",
        "performed_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_openapi_declares_the_action_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()
    path = schema["paths"]["/api/ad-accounts/{account_id}/actions"]

    assert path["post"]["operationId"] == "recordAction"
    assert path["get"]["operationId"] == "listActions"
    assert path["post"]["tags"] == ["actions"]


def test_source_is_not_an_input(offline_client: TestClient) -> None:
    """`source` 不许出现在入参里。

    放它出去，人手工登记时会随手标成「平台抓的」，而那两种来源的区别恰恰是
    `reason` 可不可信 —— 平台记不住人为什么这么调。
    """
    schema = offline_client.get("/openapi.json").json()
    request = schema["components"]["schemas"]["ActionCreateRequest"]

    assert "source" not in request["properties"]


@pytest.mark.parametrize(
    ("start", "end", "expect_since", "expect_until"),
    [
        # 单日：右端是次日零点，**不是当天 23:59:59**。写成闭区间会漏掉那一秒之内
        # 发生的操作，而漏掉的那条不会有任何迹象。
        (
            date(2026, 3, 10),
            date(2026, 3, 10),
            "2026-03-10T00:00:00-08:00",
            "2026-03-11T00:00:00-08:00",
        ),
        # 跨月
        (
            date(2026, 1, 30),
            date(2026, 2, 2),
            "2026-01-30T00:00:00-09:00",
            "2026-02-03T00:00:00-09:00",
        ),
    ],
)
def test_window_bounds_uses_the_account_timezone(
    start: date,
    end: date,
    expect_since: str,
    expect_until: str,
) -> None:
    """零点按**账户时区**取，偏移量随夏令时变。

    Anchorage 冬天是 -09:00、夏天是 -08:00 —— 两个用例的偏移量不同正是这套换算
    在做事的证据。用服务器时区去截的话，日切点附近的操作会整体归到相邻那一天。
    """
    since, until = window_bounds(_TZ, start, end)

    assert since.isoformat() == expect_since
    assert until.isoformat() == expect_until


def test_window_bounds_covers_the_dst_gap_day() -> None:
    """夏令时切换日：窗口仍然从零点到次日零点，真实长度可以不是 24 小时。

    2026-03-08 Anchorage 跳过 02:00–03:00，那天只有 23 小时。**这是刻意接受的**：
    广告数据本来就按平台的自然日下发，为一年两天的一小时去发明另一套口径，只会让
    每天的口径都变得难解释。这里钉死这个行为，免得日后有人「顺手修正」它。

    ⚠️ **断言必须先转 UTC 再相减。** 两个 aware datetime 的 tzinfo 相同时，Python
    直接按朴素时间相减、根本不看偏移量（标准库文档明写了这个行为），于是这里会
    得到 24 小时。查询本身不受影响 —— 边界原样交给驱动，转 UTC 的是它。
    """
    since, until = window_bounds(_TZ, date(2026, 3, 8), date(2026, 3, 8))

    assert since.utcoffset() != until.utcoffset()
    assert until.astimezone(UTC) - since.astimezone(UTC) == timedelta(hours=23)


@pytest.mark.integration
async def test_naive_timestamp_is_rejected(live_api: AsyncClient) -> None:
    """不带时区的时刻要 422 —— 它会被按服务器时区解释，于是悄悄偏几个小时。"""
    account_id = await _new_account(live_api, "裸时刻")

    response = await live_api.post(
        f"/api/ad-accounts/{account_id}/actions",
        json=_payload(performed_at="2026-08-20T15:30:00"),
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_future_action_is_rejected(live_api: AsyncClient) -> None:
    """落在未来的记录永远不会进任何一期日报，却看起来像是记过了。"""
    account_id = await _new_account(live_api, "未来")
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    response = await live_api.post(
        f"/api/ad-accounts/{account_id}/actions",
        json=_payload(performed_at=tomorrow),
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_reason_is_required(live_api: AsyncClient) -> None:
    """🔴 `reason` 是这张表和平台变更日志的唯一区别，空字符串也不行。"""
    account_id = await _new_account(live_api, "缺理由")

    missing = await live_api.post(
        f"/api/ad-accounts/{account_id}/actions",
        json={k: v for k, v in _payload().items() if k != "reason"},
    )
    blank = await live_api.post(
        f"/api/ad-accounts/{account_id}/actions",
        json=_payload(reason=""),
    )

    assert missing.status_code == 422
    assert blank.status_code == 422


@pytest.mark.integration
async def test_unknown_account_is_404(live_api: AsyncClient) -> None:
    response = await live_api.post("/api/ad-accounts/999999/actions", json=_payload())

    assert response.status_code == 404


@pytest.mark.integration
async def test_recorded_action_comes_back_as_manual(live_api: AsyncClient) -> None:
    """登记进去的一律是 `manual`，层级不填默认账户级。"""
    account_id = await _new_account(live_api, "登记")

    created = await live_api.post(f"/api/ad-accounts/{account_id}/actions", json=_payload())
    assert created.status_code == 201, created.text

    body = created.json()
    assert body["source"] == "manual"
    assert body["level"] == "account"
    assert body["kind"] == "budget"
    assert body["object_id"] is None


@pytest.mark.integration
async def test_list_orders_by_when_it_happened(live_api: AsyncClient) -> None:
    """按 `performed_at` 排，不是按登记时间。

    补登记的那条录入更晚、描述的事更早。按登记时间排会读不出投放的先后 —— 这里
    先登记「早发生的」再登记「晚发生的」，列表里必须是后者在前。
    """
    account_id = await _new_account(live_api, "排序")
    older = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    newer = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    for summary, performed_at in (("两天前做的", older), ("一小时前做的", newer)):
        response = await live_api.post(
            f"/api/ad-accounts/{account_id}/actions",
            json=_payload(summary=summary, performed_at=performed_at),
        )
        assert response.status_code == 201, response.text

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/actions")).json()

    assert body["total"] == 2
    assert [item["summary"] for item in body["items"]] == ["一小时前做的", "两天前做的"]


@pytest.mark.integration
async def test_window_picks_the_day_in_the_account_timezone(live_session: AsyncSession) -> None:
    """🔴 日报要的那个查询：一条操作落在**账户时区**的哪一天。

    UTC 的 08-15 06:00 在 Anchorage 是 08-14 22:00 —— 按服务器时区（CI 上是 UTC）
    去截，这条记录会归到 15 号，于是它出现在错误那一期日报里，而两期日报都不会
    报错。这条用例钉的就是「归到 14 号」。
    """
    account = await _account_row(live_session, "时区窗口")
    await action_service.record(
        live_session,
        account_id=account.id,
        kind=ActionKind.BUDGET,
        summary="日预算 500 → 800",
        reason="周末 CPM 普涨，先扛量到周一",
        performed_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
    )

    on_14 = await action_service.list_in_window(
        live_session,
        account=account,
        start=date(2026, 8, 14),
        end=date(2026, 8, 14),
    )
    on_15 = await action_service.list_in_window(
        live_session,
        account=account,
        start=date(2026, 8, 15),
        end=date(2026, 8, 15),
    )

    assert [row.summary for row in on_14] == ["日预算 500 → 800"]
    assert list(on_15) == []


@pytest.mark.integration
async def test_window_returns_actions_in_the_order_they_happened(
    live_session: AsyncSession,
) -> None:
    """日报按投放先后讲，所以这个形态是**正序**（列表接口才是倒序）。"""
    account = await _account_row(live_session, "窗口排序")
    for hour, summary in ((3, "早上关了一组"), (20, "晚上加了预算")):
        await action_service.record(
            live_session,
            account_id=account.id,
            kind=ActionKind.OTHER,
            summary=summary,
            reason="验证顺序",
            # 账户时区 08-14 当天：UTC 差 8 小时，所以这两个时刻都落在 14 号
            performed_at=datetime(2026, 8, 14, hour, 0, tzinfo=ZoneInfo(_TZ)),
        )

    rows = await action_service.list_in_window(
        live_session,
        account=account,
        start=date(2026, 8, 14),
        end=date(2026, 8, 14),
    )

    assert [row.summary for row in rows] == ["早上关了一组", "晚上加了预算"]
