"""TikTok 适配器。

**最重要的一条在最前面**：TikTok 把业务错误放在 HTTP 200 的响应体里。只判状态码
的实现会把「token 已失效」当成一次拿到零行的成功拉取 —— 而零行在看板上和「昨天
没投放」长得一模一样。整套自动拉取里最危险的失败模式就是它。

一个数据库都不起：provider 只认外部格式，这是它在分层图上和 `db` 同层的原因。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from adpilot.providers import tiktok
from adpilot.providers.base import FetchError

TOKEN = SecretStr("act.fake-token")
SINCE = date(2026, 8, 20)
UNTIL = date(2026, 8, 22)


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """把 provider 内部新建的 `AsyncClient` 换成一个走 MockTransport 的。

    provider 每次调用都自建 client（同 `llm/openai_compat.py` 的取舍：一天几次
    调用，连接池不值得进 `Resources`），所以打桩只能从这一层进。

    ⚠️ **真正的类要先存下来**：`monkeypatch.setattr` 之后 `httpx.AsyncClient`
    就是下面这个 factory 了，在它体内再引用一次就是无限递归。
    """
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    # patch 的是 httpx 模块本身 —— provider 用的就是 `httpx.AsyncClient`，同一个
    # 模块对象，不必（也不该）从 provider 模块里取那个名字。
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _ok(rows: list[dict[str, Any]], *, total_page: int = 1) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "list": rows,
            "page_info": {
                "page": 1,
                "page_size": 1000,
                "total_number": len(rows),
                "total_page": total_page,
            },
        },
    }


def _row(*, campaign_id: str, day: str, spend: str) -> dict[str, Any]:
    """一行 TikTok 报表的原始形状：dimensions / metrics 两层嵌套。"""
    return {
        "dimensions": {"campaign_id": campaign_id, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {
            "spend": spend,
            "impressions": "1000",
            "clicks": "20",
            "conversion": "1",
            "campaign_name": "示例系列",
        },
    }


# --- 🔴 HTTP 200 不代表成功 -------------------------------------------------


async def test_business_error_inside_a_200_response_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code != 0 必须抛，哪怕 HTTP 是 200。

    这是整个适配器里最要紧的一条：不抛的话，一次「token 已失效」会变成一次
    「拿到零行」的成功拉取，于是 `fetch_states` 记成功、告警不开、看板显示
    昨天花了 0 元 —— 每一环都在正常工作，结论却是错的。
    """
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 40105, "message": "x"}))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError) as exc_info:
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)

    assert exc_info.value.retryable is False


async def test_credential_errors_tell_the_operator_to_reauthorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据类错误码要在消息里说清楚该做什么。

    推到群里的只有那一行字，而「重新授权」和「等平台恢复」是两种完全不同的动作。
    """
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 40105, "message": "x"}))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError, match="重新授权"):
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)


async def test_server_side_error_codes_are_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 50000, "message": "x"}))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError) as exc_info:
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)

    assert exc_info.value.retryable is True


async def test_unknown_error_codes_default_to_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没见过的错误码兜底成**不可重试**。

    理由在 `FetchError` 的 docstring：误判成不可重试的代价是一条多余的告警，
    反过来的代价是数据静默停更好几天。
    """
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 41234, "message": "x"}))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError) as exc_info:
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)

    assert exc_info.value.retryable is False


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_throttling_and_server_errors_are_retryable(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    _stub(monkeypatch, lambda _request: httpx.Response(status_code, text="nope"))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError) as exc_info:
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)

    assert exc_info.value.retryable is True


# --- 产物形状 ---------------------------------------------------------------


async def test_rows_are_flattened_without_renaming_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两层嵌套摊成一层，**键名一个字都不改**。

    改名会把当时的映射规则永久烧进快照，而快照存在的全部意义就是「映射规则改了
    还能重跑」。映射发生在 `services/field_maps.py`，不在这里。
    """
    rows = [_row(campaign_id="c1", day="2026-08-20", spend="9.37")]
    _stub(monkeypatch, lambda _request: httpx.Response(200, json=_ok(rows)))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    result = await provider.fetch(external_id="123", level="campaign", since=SINCE, until=SINCE)

    (day,) = result.days
    assert day.rows == [
        {
            "campaign_id": "c1",
            "stat_time_day": "2026-08-20 00:00:00",
            "spend": "9.37",
            "impressions": "1000",
            "clicks": "20",
            "conversion": "1",
            "campaign_name": "示例系列",
        }
    ]


async def test_rows_are_grouped_by_day(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row(campaign_id="c1", day="2026-08-21", spend="1.00"),
        _row(campaign_id="c2", day="2026-08-20", spend="2.00"),
        _row(campaign_id="c2", day="2026-08-21", spend="3.00"),
    ]
    _stub(monkeypatch, lambda _request: httpx.Response(200, json=_ok(rows)))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    result = await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)

    # 升序，且同一天的行归在一起。
    assert [day.stat_date for day in result.days] == [date(2026, 8, 20), date(2026, 8, 21)]
    assert [len(day.rows) for day in result.days] == [1, 2]


async def test_a_row_without_a_parsable_day_raises_instead_of_being_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 日期认不出来的行**报错，不静默丢弃**。

    丢一行的后果是那天少一个对象的花费，而账户总数看起来仍然很正常 —— 这种错
    发现不了。（CSV 那边的 `skipped_rows` 是给「末尾 Total 行」的，那是一种
    已知且可解释的跳过，不是一回事。）
    """
    rows = [{"dimensions": {"campaign_id": "c1", "stat_time_day": "昨天"}, "metrics": {}}]
    _stub(monkeypatch, lambda _request: httpx.Response(200, json=_ok(rows)))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError, match="stat_time_day"):
        await provider.fetch(external_id="123", level="campaign", since=SINCE, until=UNTIL)


async def test_all_pages_are_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    """翻页要翻完 —— 只取第一页的症状是「大账户的数据莫名其妙少一截」。"""
    pages = {
        "1": _ok([_row(campaign_id="c1", day="2026-08-20", spend="1.00")], total_page=2),
        "2": _ok([_row(campaign_id="c2", day="2026-08-20", spend="2.00")], total_page=2),
    }
    _stub(
        monkeypatch,
        lambda request: httpx.Response(200, json=pages[request.url.params["page"]]),
    )
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    result = await provider.fetch(external_id="123", level="campaign", since=SINCE, until=SINCE)

    (day,) = result.days
    assert len(day.rows) == 2


async def test_requested_metrics_include_the_configured_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额外指标要真的进请求。

    它们走配置正是因为**请求一个不存在的 metric 是整个请求 400** —— 收入类指标
    最容易改名，而改名那天我们要能只改环境变量。
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=_ok([]))

    _stub(monkeypatch, handler)
    provider = tiktok.TikTokProvider(
        access_token=TOKEN, extra_metrics=("total_complete_order_revenue",)
    )

    await provider.fetch(external_id="123", level="campaign", since=SINCE, until=SINCE)

    assert "total_complete_order_revenue" in seen["metrics"]
    # 层级翻成平台方言，日期维度固定按天（不是按小时再自己聚合）。
    assert seen["data_level"] == "AUCTION_CAMPAIGN"
    assert "stat_time_day" in seen["dimensions"]


async def test_unknown_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, lambda _request: httpx.Response(200, json=_ok([])))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError, match="层级"):
        await provider.fetch(external_id="123", level="creative", since=SINCE, until=SINCE)


# --- 余额 -------------------------------------------------------------------


async def test_balance_is_decimal_all_the_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """金额一路 `Decimal`，不经 float（CLAUDE.md 硬规矩 6）。"""
    body = {"code": 0, "data": {"list": [{"balance": "39.8012", "currency": "usd"}]}}
    _stub(monkeypatch, lambda _request: httpx.Response(200, json=body))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    snapshot = await provider.fetch_balance(external_id="123")

    assert snapshot.available == Decimal("39.8012")
    # 币种规范成大写：ISO 4217 是大写，而账户上存的也是大写 —— 两边对不上时
    # 「余额和消耗是不是同一种货币」这个判断会失败。
    assert snapshot.currency == "USD"
    assert snapshot.captured_at.tzinfo is not None


async def test_balance_for_an_unknown_account_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查不到 = 账户 ID 不对或 token 没这个账户的权限，都是人配错了。"""
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 0, "data": {"list": []}}))
    provider = tiktok.TikTokProvider(access_token=TOKEN)

    with pytest.raises(FetchError) as exc_info:
        await provider.fetch_balance(external_id="123")

    assert exc_info.value.retryable is False


# --- 授权 -------------------------------------------------------------------


async def test_auth_code_is_posted_in_the_body_not_the_query_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 换 token 必须走 POST body。

    `auth_code` 和 app secret 走查询串会被 nginx 和各级代理原样记进访问日志 ——
    那是一份谁都能读的凭据副本。
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["query"] = str(request.url.query)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"access_token": "act.new", "advertiser_ids": ["7001", 7002]}},
        )

    _stub(monkeypatch, handler)

    token, advertiser_ids = await tiktok.exchange_auth_code(
        app_id="app", app_secret=SecretStr("secret"), auth_code="code"
    )

    assert seen["method"] == "POST"
    assert "code" not in seen["query"]
    assert token.get_secret_value() == "act.new"
    # 平台混着给字符串和数字，统一成字符串 —— 账户 ID 一律按字符串比对。
    assert advertiser_ids == ("7001", "7002")


async def test_authorization_without_a_token_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth_code` 是一次性的，重试只会拿到「已使用」。"""
    _stub(monkeypatch, lambda _request: httpx.Response(200, json={"code": 0, "data": {}}))

    with pytest.raises(FetchError) as exc_info:
        await tiktok.exchange_auth_code(
            app_id="app", app_secret=SecretStr("secret"), auth_code="code"
        )

    assert exc_info.value.retryable is False
