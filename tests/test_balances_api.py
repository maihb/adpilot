"""余额快照与余额告警。

集成那几条验的是**只有真连上数据库才知道**的事：日均消耗从 `daily_metrics` 里
按哪个层级捞、回看窗口怎么截、以及「余额告急」这条告警端到端能不能触发 —— 最后
这条正是 D6–D8 的验收标准之一。

规则本身的边界（分母为 0、余额见底、阈值含不含等号）在 `test_rules_balance.py`
里用纯函数覆盖，这里不重复。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

# 账户时区固定用它：真实案例里的日切点差异就来自这种时区，不是随手挑的。
_TZ = "America/Anchorage"


async def _new_account(api: AsyncClient, suffix: str) -> int:
    client = await api.post("/api/clients", json={"name": f"测试客户-余额-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "tiktok",
            "external_id": f"acct-balance-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": _TZ,
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


def _yesterday_back(days: int) -> str:
    """账户时区下「昨天」往前数 days 天的那一天。

    用固定日期写死会让这套用例在某天之后集体过期（窗口是相对今天算的），所以
    这里跟着服务端同一套口径推 —— 服务端也是按账户时区取今天再减一天。
    """
    today = datetime.now(ZoneInfo(_TZ)).date()
    return (today - timedelta(days=days + 1)).isoformat()


def _spend_csv(*rows: tuple[str, str]) -> bytes:
    header = "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,Link clicks\n"
    body = "".join(f"{day},cmp-1,测试系列,{spend},1000,50\n" for day, spend in rows)
    return (header + body).encode()


async def _import_and_normalize(api: AsyncClient, account_id: int, content: bytes) -> None:
    imported = await api.post(
        "/api/imports",
        files={"file": ("report.csv", content, "text/csv")},
        data={"account_id": str(account_id), "level": "campaign"},
    )
    assert imported.status_code == 201, imported.text

    # 导入只落快照, 归一化是另一步。集成环境里没有 worker 在跑, 所以走同步接口 ——
    # 两条链路调的是同一个服务函数, 这里验的也不是排队那一段。
    normalized = await api.post(f"/api/ad-accounts/{account_id}/normalize")
    assert normalized.status_code == 200, normalized.text


async def _record(
    api: AsyncClient,
    account_id: int,
    available: str,
    *,
    hours_ago: int = 1,
) -> None:
    captured = datetime.now(UTC) - timedelta(hours=hours_ago)
    response = await api.post(
        f"/api/ad-accounts/{account_id}/balances",
        json={"available": available, "captured_at": captured.isoformat()},
    )
    assert response.status_code == 201, response.text


def test_openapi_declares_the_balance_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    record = schema["paths"]["/api/ad-accounts/{account_id}/balances"]["post"]
    runway = schema["paths"]["/api/ad-accounts/{account_id}/balance-runway"]["get"]
    alerts = schema["paths"]["/api/alerts/balances"]["get"]

    assert record["operationId"] == "recordBalance"
    assert runway["operationId"] == "getBalanceRunway"
    assert alerts["operationId"] == "listBalanceAlerts"
    assert record["tags"] == ["balances"]


@pytest.mark.integration
async def test_naive_timestamp_is_rejected(live_api: AsyncClient) -> None:
    """不带时区的时刻要 422。

    放行的话它会被当成服务器本地时区存进 timestamptz，而服务器时区和账户时区
    常常不是一回事 —— 于是「昨天下午看到的余额」悄悄偏了几个小时。偏几小时不会
    让任何东西报错，只会让日报里的口径说不清。
    """
    account_id = await _new_account(live_api, "裸时刻")

    response = await live_api.post(
        f"/api/ad-accounts/{account_id}/balances",
        json={"available": "100", "captured_at": "2026-08-20T10:00:00"},
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_negative_balance_is_rejected(live_api: AsyncClient) -> None:
    """负余额一定是填错了。0 是合法的（真花光了）。"""
    account_id = await _new_account(live_api, "负数")

    response = await live_api.post(
        f"/api/ad-accounts/{account_id}/balances",
        json={"available": "-1", "captured_at": datetime.now(UTC).isoformat()},
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_currency_comes_from_the_account(live_api: AsyncClient) -> None:
    """币种不让调用方传 —— 传了早晚会出现「账户 USD、余额录成 CNY」的一条快照。"""
    account_id = await _new_account(live_api, "币种")
    await _record(live_api, account_id, "500")

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balances")).json()

    assert body["total"] == 1
    assert body["items"][0]["currency"] == "USD"


@pytest.mark.integration
async def test_same_instant_twice_is_a_conflict(live_api: AsyncClient) -> None:
    """挡的是「点了两次提交」。两条一样的快照不会算错，但会让人核对时怀疑自己看错。"""
    account_id = await _new_account(live_api, "重复")
    captured = datetime.now(UTC).isoformat()
    payload = {"available": "500", "captured_at": captured}

    first = await live_api.post(f"/api/ad-accounts/{account_id}/balances", json=payload)
    second = await live_api.post(f"/api/ad-accounts/{account_id}/balances", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.integration
async def test_snapshots_are_append_only(live_api: AsyncClient) -> None:
    """录第二条是第二行，不覆盖 —— 「上周五还剩多少」是个会被回头追问的问题。"""
    account_id = await _new_account(live_api, "只增")
    await _record(live_api, account_id, "500", hours_ago=48)
    await _record(live_api, account_id, "300", hours_ago=1)

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balances")).json()

    assert body["total"] == 2
    # 最新的在前
    assert [item["available"] for item in body["items"]] == ["300.0000", "500.0000"]


@pytest.mark.integration
async def test_never_recorded_returns_null_not_a_fake_alert(live_api: AsyncClient) -> None:
    """🔴 从没录过余额 → `null`，不是 404 也不是「0 天」。

    账户是存在的，只是没有余额数据。把「没录过」和「余额是 0」混起来，会让每个
    刚建好的账户立刻冒出一条假告警 —— 而假告警会让人对整个列表脱敏。
    """
    account_id = await _new_account(live_api, "没录过")

    response = await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")

    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.integration
async def test_unknown_account_is_404(live_api: AsyncClient) -> None:
    """账户不存在才是 404 —— 与上一条的 null 区分开，前端才知道该提示什么。"""
    assert (await live_api.get("/api/ad-accounts/999999/balance-runway")).status_code == 404


@pytest.mark.integration
async def test_runway_divides_balance_by_the_daily_average(live_api: AsyncClient) -> None:
    """近 N 天日均消耗算得对，可撑天数才有意义。

    导 3 天、每天 100，日均就是 100（分母是**有数据的天数** 3，不是窗口长度 7）。
    余额 1000 → 10 天。用 7 当分母的话日均约 42.9、可撑天数会变成 23.3。
    """
    account_id = await _new_account(live_api, "日均")
    await _import_and_normalize(
        live_api,
        account_id,
        _spend_csv(
            (_yesterday_back(0), "100"),
            (_yesterday_back(1), "100"),
            (_yesterday_back(2), "100"),
        ),
    )
    await _record(live_api, account_id, "1000")

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()

    assert Decimal(body["avg_daily_spend"]) == Decimal(100)
    assert Decimal(body["days_left"]) == Decimal(10)
    assert body["days_with_data"] == 3
    assert body["is_alerting"] is False


@pytest.mark.integration
async def test_spend_outside_the_window_is_ignored(live_api: AsyncClient) -> None:
    """窗口是「昨天往前 7 天」，更早的花费不参与。

    不截窗口的话，一个跑过大促、之后停投的账户会一直背着当时的高日均，于是余额
    明明够用却天天告警。
    """
    account_id = await _new_account(live_api, "窗口")
    await _import_and_normalize(
        live_api,
        account_id,
        _spend_csv(
            (_yesterday_back(0), "100"),
            # 30 天前的一笔大额消耗，应当被完全忽略
            (_yesterday_back(30), "99999"),
        ),
    )
    await _record(live_api, account_id, "1000")

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()

    assert body["days_with_data"] == 1
    assert Decimal(body["avg_daily_spend"]) == Decimal(100)


@pytest.mark.integration
async def test_alert_fires_when_the_balance_runs_low(live_api: AsyncClient) -> None:
    """**D6–D8 的验收标准之一：余额告警能触发。**

    日均 100、余额 250 → 2.5 天，低于 3 天的阈值。这个账户必须出现在告警清单里，
    而且带得上「还剩几天」这个能直接发给客户的数字。
    """
    account_id = await _new_account(live_api, "告警")
    await _import_and_normalize(
        live_api,
        account_id,
        _spend_csv((_yesterday_back(0), "100"), (_yesterday_back(1), "100")),
    )
    await _record(live_api, account_id, "250")

    single = (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()
    assert Decimal(single["days_left"]) == Decimal("2.5")
    assert single["is_alerting"] is True
    assert Decimal(single["threshold_days"]) == Decimal(3)

    listed = (await live_api.get("/api/alerts/balances")).json()
    assert account_id in [item["account_id"] for item in listed["items"]]


@pytest.mark.integration
async def test_paused_account_is_not_alerted(live_api: AsyncClient) -> None:
    """近期没花钱 → 无定义，不告警。没有消耗就不会归零。"""
    account_id = await _new_account(live_api, "停投")
    await _record(live_api, account_id, "10")

    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()

    assert body["days_left"] is None
    assert body["is_alerting"] is False
    assert account_id not in [
        item["account_id"] for item in (await live_api.get("/api/alerts/balances")).json()["items"]
    ]


@pytest.mark.integration
async def test_inactive_accounts_stay_out_of_the_alert_list(live_api: AsyncClient) -> None:
    """停止合作的客户余额多少都不重要，混在列表里只会让人学会忽略这个列表。"""
    account_id = await _new_account(live_api, "已停用")
    await _import_and_normalize(live_api, account_id, _spend_csv((_yesterday_back(0), "100")))
    await _record(live_api, account_id, "10")

    assert (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()[
        "is_alerting"
    ] is True

    deactivated = await live_api.patch(f"/api/ad-accounts/{account_id}", json={"is_active": False})
    assert deactivated.status_code == 200, deactivated.text

    listed = (await live_api.get("/api/alerts/balances")).json()
    assert account_id not in [item["account_id"] for item in listed["items"]]


@pytest.mark.integration
async def test_mixed_levels_do_not_double_count_spend(live_api: AsyncClient) -> None:
    """🔴 同一天既有账户级又有系列级的数据时，**不能加起来**。

    加起来就是双倍花费 → 日均翻倍 → 可撑天数腰斩 → 一条凭空冒出来的告警。
    挑法是「取有数据的最高层级」，所以这里应当只认账户级那 100。
    """
    account_id = await _new_account(live_api, "双层级")
    day = _yesterday_back(0)

    await _import_and_normalize(live_api, account_id, _spend_csv((day, "100")))

    account_level = (
        "Day,Account ID,Account name,Amount spent (USD),Impressions,Link clicks\n"
        f"{day},acct-balance-双层级,测试账户,100,1000,50\n"
    ).encode()
    imported = await live_api.post(
        "/api/imports",
        files={"file": ("account.csv", account_level, "text/csv")},
        data={"account_id": str(account_id), "level": "account"},
    )
    assert imported.status_code == 201, imported.text
    assert (await live_api.post(f"/api/ad-accounts/{account_id}/normalize")).status_code == 200

    await _record(live_api, account_id, "1000")
    body = (await live_api.get(f"/api/ad-accounts/{account_id}/balance-runway")).json()

    # 加起来的话日均是 200、可撑天数 5.0
    assert Decimal(body["avg_daily_spend"]) == Decimal(100)
    assert Decimal(body["days_left"]) == Decimal(10)
