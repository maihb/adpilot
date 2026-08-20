"""告警巡检：状态机、去重、推送。

**这里验的是对账逻辑，不是判定逻辑。** 阈值含不含等号、分母为 0 怎么办，全在
`test_rules_balance.py` 和 `test_rules_anomaly.py` 里用纯函数覆盖。这一层要回答的
是另一组问题：同一件事巡检十次会不会变成十条？问题消失了会不会自动收掉？推送会不会
每轮重发？

最后那个是这套状态机存在的**全部理由** —— 巡检每小时跑一次，一个持续三天的余额问题
会被发现七十多次。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from pydantic import SecretStr

from adpilot.config import Settings
from adpilot.notifiers import webhook

_TZ = "America/Anchorage"


async def _new_account(api: AsyncClient, suffix: str) -> int:
    client = await api.post("/api/clients", json={"name": f"测试客户-告警-{suffix}"})
    assert client.status_code == 201, client.text

    account = await api.post(
        "/api/ad-accounts",
        json={
            "client_id": client.json()["id"],
            "platform": "tiktok",
            "external_id": f"acct-alert-{suffix}",
            "name": f"测试账户-{suffix}",
            "currency": "USD",
            "timezone": _TZ,
        },
    )
    assert account.status_code == 201, account.text
    return int(account.json()["id"])


def _day(days_before_yesterday: int) -> str:
    today = datetime.now(ZoneInfo(_TZ)).date()
    return (today - timedelta(days=days_before_yesterday + 1)).isoformat()


def _csv(*rows: tuple[str, str, str]) -> bytes:
    header = "Day,Campaign ID,Campaign name,Amount spent (USD),Impressions,Link clicks,Results\n"
    body = "".join(f"{day},cmp-1,测试系列,{spend},1000,50,{conv}\n" for day, spend, conv in rows)
    return (header + body).encode()


async def _load(api: AsyncClient, account_id: int, content: bytes) -> None:
    imported = await api.post(
        "/api/imports",
        files={"file": ("report.csv", content, "text/csv")},
        data={"account_id": str(account_id), "level": "campaign"},
    )
    assert imported.status_code == 201, imported.text
    assert (await api.post(f"/api/ad-accounts/{account_id}/normalize")).status_code == 200


async def _record_balance(api: AsyncClient, account_id: int, available: str) -> None:
    response = await api.post(
        f"/api/ad-accounts/{account_id}/balances",
        json={"available": available, "captured_at": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == 201, response.text


async def _sweep(api: AsyncClient) -> dict[str, int]:
    response = await api.post("/api/alerts/sweep")
    assert response.status_code == 200, response.text
    summary: dict[str, int] = response.json()
    return summary


async def _alerts_of(api: AsyncClient, account_id: int) -> list[dict[str, Any]]:
    response = await api.get("/api/alerts", params={"account_id": account_id})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def test_openapi_declares_the_alert_operations(offline_client: TestClient) -> None:
    schema = offline_client.get("/openapi.json").json()

    assert schema["paths"]["/api/alerts"]["get"]["operationId"] == "listAlerts"
    assert schema["paths"]["/api/alerts/sweep"]["post"]["operationId"] == "sweepAlerts"
    assert schema["paths"]["/api/alerts/balances"]["get"]["tags"] == ["alerts"]


@pytest.mark.integration
async def test_sweep_opens_an_alert_when_the_balance_runs_low(live_api: AsyncClient) -> None:
    """巡检发现余额告急 → 开一条，带上能直接贴进日报的人话摘要和触发时的数字。"""
    account_id = await _new_account(live_api, "开单")
    await _load(live_api, account_id, _csv((_day(0), "100", "5"), (_day(1), "100", "5")))
    await _record_balance(live_api, account_id, "250")

    summary = await _sweep(live_api)
    assert summary["opened"] >= 1

    alerts = await _alerts_of(live_api, account_id)
    balance_alerts = [a for a in alerts if a["kind"] == "balance_low"]
    assert len(balance_alerts) == 1

    alert = balance_alerts[0]
    assert alert["status"] == "open"
    assert alert["subject"] == "balance"
    assert "2.5" in alert["message"]
    # detail 是**触发时的快照**：余额之后会变，而日报要写「触发时还剩几天」
    assert alert["detail"]["days_left"] == "2.5"
    assert alert["detail"]["currency"] == "USD"


@pytest.mark.integration
async def test_sweeping_twice_does_not_duplicate_the_alert(live_api: AsyncClient) -> None:
    """🔴 这是整套状态机存在的理由。

    巡检每小时跑一次，一个持续三天的问题会被发现七十多次。每次写一条的话，人第一天
    就会把这个清单关掉。
    """
    account_id = await _new_account(live_api, "去重")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")

    await _sweep(live_api)
    assert len(await _alerts_of(live_api, account_id)) == 1

    # ⚠️ 摘要里的数字是**全局**的（巡检扫的是所有在投账户），所以「开了几条」这类
    # 断言必须按账户收窄 —— 开发机上那个库里往往躺着别的账户，而它们也会被扫到。
    # 「这一轮没有新开的」倒是全局也成立：两次巡检之间没有任何东西变过。
    second = await _sweep(live_api)
    third = await _sweep(live_api)

    assert second["opened"] == 0
    assert third["opened"] == 0
    assert len(await _alerts_of(live_api, account_id)) == 1


@pytest.mark.integration
async def test_opened_at_survives_later_sweeps(live_api: AsyncClient) -> None:
    """`opened_at` 不随巡检更新，`last_seen_at` 才是每轮刷新的那个。

    「这个问题从什么时候开始的」是日报里要写的东西，被覆盖掉就再也算不出来。
    """
    account_id = await _new_account(live_api, "起点")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")

    await _sweep(live_api)
    opened_at = (await _alerts_of(live_api, account_id))[0]["opened_at"]

    await _sweep(live_api)
    after = (await _alerts_of(live_api, account_id))[0]

    assert after["opened_at"] == opened_at
    assert after["last_seen_at"] >= opened_at


@pytest.mark.integration
async def test_alert_resolves_itself_when_the_problem_goes_away(live_api: AsyncClient) -> None:
    """充值之后，下一轮巡检要把它收掉 —— 而不是留着让人手动点掉。

    留着的话，清单上很快会全是已经解决的东西，然后没人再看它。
    """
    account_id = await _new_account(live_api, "自愈")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")
    await _sweep(live_api)

    # 充值：录一条新的、更高的余额快照
    await _record_balance(live_api, account_id, "99999")
    summary = await _sweep(live_api)

    assert summary["resolved"] >= 1
    alert = (await _alerts_of(live_api, account_id))[0]
    assert alert["status"] == "resolved"
    assert alert["resolved_at"] is not None


@pytest.mark.integration
async def test_a_resolved_problem_can_open_again(live_api: AsyncClient) -> None:
    """再次告急要开**新的一条**，不是把旧的翻回 open。

    部分唯一索引只管 open 的行，所以同一个 subject 的历史可以有很多条 —— 而
    「这次是什么时候开始的」需要一个新的 `opened_at`。
    """
    account_id = await _new_account(live_api, "复发")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")
    await _sweep(live_api)

    await _record_balance(live_api, account_id, "99999")
    await _sweep(live_api)

    await _record_balance(live_api, account_id, "50")
    await _sweep(live_api)

    alerts = [a for a in await _alerts_of(live_api, account_id) if a["kind"] == "balance_low"]
    assert len(alerts) == 2
    assert sorted(a["status"] for a in alerts) == ["open", "resolved"]


@pytest.mark.integration
async def test_spend_anomaly_is_measured_against_the_same_weekday(live_api: AsyncClient) -> None:
    """周同比：昨天 vs 上周同一天。

    用日环比的话，周末 CPM 普涨会每周固定制造两批假告警 —— 而设计文档举的那个日报
    例子原文就是「成本上升是周末 CPM 普涨，未做调整」。
    """
    account_id = await _new_account(live_api, "花费异动")
    await _load(live_api, account_id, _csv((_day(0), "300", "5"), (_day(7), "100", "5")))

    await _sweep(live_api)

    anomalies = [a for a in await _alerts_of(live_api, account_id) if a["kind"] == "metric_anomaly"]
    spend = [a for a in anomalies if a["subject"] == "metric:spend"]
    assert len(spend) == 1
    assert spend[0]["detail"]["change_ratio"] == "2.0000"
    assert spend[0]["detail"]["direction"] == "up"


@pytest.mark.integration
async def test_spend_and_cpa_anomalies_do_not_overwrite_each_other(live_api: AsyncClient) -> None:
    """🔴 `subject` 带上指标名，否则两条异动会互相顶掉。

    部分唯一索引认的是 (账户, 种类, subject)，少了指标名的话「花费异动」和「CPA
    异动」在数据库看来是同一件事，后写的那条会撞唯一键。
    """
    account_id = await _new_account(live_api, "两条异动")
    # 花费翻三倍、转化数不变 → 花费和 CPA 一起恶化
    await _load(live_api, account_id, _csv((_day(0), "300", "5"), (_day(7), "100", "5")))

    await _sweep(live_api)

    subjects = {
        a["subject"]
        for a in await _alerts_of(live_api, account_id)
        if a["kind"] == "metric_anomaly"
    }
    assert subjects == {"metric:spend", "metric:cpa"}


@pytest.mark.integration
async def test_no_baseline_means_no_anomaly(live_api: AsyncClient) -> None:
    """上周同日没有数据 → 整个跳过，不拿凑合的基线硬判。

    新账户前一周必然如此，而那时报出来的每一条都是噪音。
    """
    account_id = await _new_account(live_api, "无基线")
    await _load(live_api, account_id, _csv((_day(0), "9999", "1")))

    await _sweep(live_api)

    anomalies = [a for a in await _alerts_of(live_api, account_id) if a["kind"] == "metric_anomaly"]
    assert anomalies == []


@pytest.mark.integration
async def test_inactive_accounts_are_not_swept(live_api: AsyncClient) -> None:
    """停投的账户不巡检 —— 停止合作的客户余额多少都不重要。"""
    account_id = await _new_account(live_api, "停用")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "10")

    deactivated = await live_api.patch(f"/api/ad-accounts/{account_id}", json={"is_active": False})
    assert deactivated.status_code == 200, deactivated.text

    await _sweep(live_api)

    assert await _alerts_of(live_api, account_id) == []


@pytest.mark.integration
async def test_open_alerts_sort_before_resolved_ones(live_api: AsyncClient) -> None:
    """打开这张表的人九成是来看待办的，所以 open 顶到最前面。"""
    account_id = await _new_account(live_api, "排序")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")
    await _sweep(live_api)
    await _record_balance(live_api, account_id, "99999")
    await _sweep(live_api)
    await _record_balance(live_api, account_id, "50")
    await _sweep(live_api)

    statuses = [a["status"] for a in await _alerts_of(live_api, account_id)]
    assert statuses[0] == "open"


@pytest.mark.integration
async def test_alerts_can_be_filtered_by_status(live_api: AsyncClient) -> None:
    account_id = await _new_account(live_api, "筛状态")
    await _load(live_api, account_id, _csv((_day(0), "100", "5")))
    await _record_balance(live_api, account_id, "100")
    await _sweep(live_api)

    response = await live_api.get(
        "/api/alerts", params={"account_id": account_id, "status": "resolved"}
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0


# --- 推送 -------------------------------------------------------------------


async def test_webhook_is_skipped_when_not_configured() -> None:
    """没配 webhook 是**正常状态**，不是故障。

    开源使用者未必有 webhook，而一个「没配通知就起不来」的系统不符合「陌生人
    clone 下来最容易跑起来」这条判断标准。
    """
    settings = Settings(_env_file=None)

    assert settings.alerts_are_pushed is False
    assert await webhook.send(settings.alert_webhook_url, {"message": "x"}) is False


async def test_webhook_url_never_reaches_a_repr() -> None:
    """🔴 webhook 地址里带着 key，等于凭据。

    一次日志、一次异常栈都可能把整个 Settings 打出来，而日志是最容易被顺手贴进
    issue 的东西 —— 贴出去的那一刻这个 webhook 就属于所有人了。
    """
    secret = "https://example.invalid/hook?key=不该出现的东西"
    settings = Settings(_env_file=None, alert_webhook_url=SecretStr(secret))

    assert settings.alerts_are_pushed is True
    assert secret not in repr(settings)
    assert "不该出现的东西" not in repr(settings.alert_webhook_url)
