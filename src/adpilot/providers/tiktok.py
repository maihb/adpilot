"""TikTok Marketing API v1.3 适配器。

拉三样东西里的两样：**日指标**（`/report/integrated/get/`）和**余额**
（`/advertiser/info/`）。投放状态是第二步的事（[设计文档第七节][design]）。

[design]: ../../../docs/design/2026-08-25-ads-api-fetch.md

## 🔴 请求的 metrics 清单是可配置的，这不是过度设计

平台的指标名会改、会分版本、电商类指标还随着 TikTok Shop 的功能演进换名字。而
**请求一个不存在的 metric，TikTok 不会忽略它，是整个请求 400** —— 一个字段名写
错，当天所有账户一行数据都拉不到。

所以核心集合（花费、展示、点击、转化）写死在下面，**收入类指标走配置**
（`TIKTOK_EXTRA_METRICS`）：它们正是最容易改名的那一批，而沙盒跑通之前我们并不
知道当前版本管 GMV 叫什么。等实测确认了字段名，填进环境变量就生效，不必改代码、
不必重新部署镜像。

## 一个字段都不改名

响应是 `dimensions` / `metrics` 两层嵌套，这里把它**摊平成一层**再交出去 —— 摊平
只是去掉一层容器，键名一个字没动。改名会把当时的映射规则永久烧进快照，而快照
存在的全部意义就是「映射规则改了还能重跑」（`db/mongo.py` 的模块 docstring）。

映射发生在 `services/field_maps.py`，那是唯一的收口点。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from pydantic import SecretStr

from adpilot.providers.base import AccountBalance, FetchError, ParseResult, RawRows

log = structlog.get_logger(__name__)

#: 生产环境的 API 根地址。沙盒是另一个地址，由配置覆盖 —— 审核通过之前只有沙盒
#: 能用，而两者的差别应该只有这一个值。
DEFAULT_BASE_URL: Final = "https://business-api.tiktok.com/open_api/v1.3"

#: OAuth 授权页。人在浏览器里打开它、选账户、点同意，平台带着 `auth_code` 跳回
#: 我们的回调地址。**不在上面那个 base_url 之下**，它是门户页不是 API。
AUTHORIZE_URL: Final = "https://business-api.tiktok.com/portal/auth"

#: 单次请求超时。比 LLM 那 60 秒短得多：报表接口是查询，正常在几秒内返回，而
#: 这是排期任务里的一环 —— 一个卡住的请求会把 worker 的槽位占住，后面的账户
#: 全都排在它后面。
TIMEOUT_SECONDS: Final = 30.0

#: 分页上限。TikTok 允许到 1000，取满是因为翻页要多发请求，而请求数才是限流额度
#: 的计量单位。
PAGE_SIZE: Final = 1000

#: 翻页次数的硬上限。防的是「响应里的 total_page 不可信」导致的死循环 —— 一个
#: 永不结束的任务比一个失败的任务难发现得多。100 页 × 1000 行远超任何真实账户
#: 单次拉取的量。
MAX_PAGES: Final = 100

#: 归一化层级 → TikTok 的 `data_level` 与对象维度。
#:
#: 键是 `MetricLevel` 的值（本模块够不着那个枚举，见 `base.py` 的分层说明）。
#: 「adgroup」在 TikTok 叫 ad group、在 Meta 叫 ad set —— 归一化到同一个词是
#: `MetricLevel` 的职责，这里只负责翻回平台方言。
_LEVELS: Final[dict[str, tuple[str, str, str]]] = {
    # level: (data_level, 对象维度字段, 对象名字段)
    "account": ("AUCTION_ADVERTISER", "advertiser_id", "advertiser_name"),
    "campaign": ("AUCTION_CAMPAIGN", "campaign_id", "campaign_name"),
    "adgroup": ("AUCTION_ADGROUP", "adgroup_id", "adgroup_name"),
    "ad": ("AUCTION_AD", "ad_id", "ad_name"),
}

#: 每次都请求的核心指标。**只放最保守的一批** —— 它们是 BASIC 报表从 v1.2 起
#: 就没变过的字段，写错一个整个请求就 400。
#:
#: ⚠️ `conversion` 是单数，不是 `conversions`。取的是账户在后台选定的那个转化
#: 事件（口径见 glossary，那条现在标着「待定」）。
CORE_METRICS: Final = ("spend", "impressions", "clicks", "conversion")

#: 时间维度。日切按**广告账户时区**，与 `daily_metrics.stat_date` 的口径一致 ——
#: 这不是巧合，是选它的理由：换成 `stat_time_hour` 再自己聚合，就等于在我们这边
#: 重新实现一遍平台的日切，而夏令时那天会错。
_DIMENSION_DAY: Final = "stat_time_day"

#: 认证/授权类错误码：**重试一万次也还是这个结果**，而且需要人去重新授权。
#: 单独成集合是为了让告警能说出「去重新授权」而不是干巴巴的「拉取失败」。
_CREDENTIAL_CODES: Final = frozenset({40001, 40100, 40104, 40105, 40110})

#: 明确可以重试的平台错误码：服务端自己的问题。
_RETRYABLE_CODES: Final = frozenset({50000, 50002})


class TikTokProvider:
    """一个 TikTok 广告主账户的读取入口。

    构造参数收零件而不是 `Settings`：这一层不认识应用配置（同
    `llm/openai_compat.py`）。取值那一步在 `services/` 里。

    **一个实例绑一个 access_token，不绑 advertiser_id** —— 一次授权换来的 token
    覆盖一批广告主账户（`platform_credentials` 与 `ad_accounts` 是一对多，见设计
    文档 5.3），账户 ID 是每次调用的参数。
    """

    #: 与将来可能出现的「TikTok CSV 导出」provider 必须是两个名字：它们的字段
    #: 形状不一样，而这个值落进快照后，归一化要靠它知道该怎么读那份 payload。
    name = "tiktok_api"

    def __init__(
        self,
        *,
        access_token: SecretStr,
        base_url: str = DEFAULT_BASE_URL,
        extra_metrics: tuple[str, ...] = (),
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._access_token = access_token
        # 末尾斜杠有没有都行 —— 从文档里拷过来的地址两种都有，而拼出 `//report`
        # 的症状是 404，跟斜杠八竿子打不着（同 openai_compat）。
        self._base_url = base_url.rstrip("/")
        self._extra_metrics = extra_metrics
        self._timeout = timeout

    async def fetch(
        self,
        *,
        external_id: str,
        level: str,
        since: date,
        until: date,
    ) -> ParseResult:
        """拉 `[since, until]`（两端都含）的日指标，按天分组。

        区间是闭的，因为 TikTok 的 `start_date` / `end_date` 就是闭区间。
        """
        data_level, object_dimension, object_name_field = _level_spec(level)
        metrics = [*CORE_METRICS, object_name_field, *self._extra_metrics]

        params: dict[str, Any] = {
            "advertiser_id": external_id,
            "report_type": "BASIC",
            "data_level": data_level,
            # 这两个是 JSON 字符串而不是重复的 query 参数 —— TikTok 的约定，
            # 传成数组会被当成一个字面量字符串，报「维度不合法」。
            "dimensions": json.dumps([object_dimension, _DIMENSION_DAY]),
            "metrics": json.dumps(metrics),
            "start_date": since.isoformat(),
            "end_date": until.isoformat(),
            "page_size": PAGE_SIZE,
        }

        rows = await self._fetch_all_pages(params)
        return ParseResult(days=_group_by_day(rows), skipped_rows=0)

    async def fetch_balance(self, *, external_id: str) -> AccountBalance:
        """拉当前可用余额。

        ⚠️ **`captured_at` 只能填拉取时刻** —— TikTok 不告诉我们这个余额是什么
        时候的。差别在「刚充完值那几分钟」会显现，所以落库时 `note` 要写明这是
        自动拉取的，好让人在核对时知道该按哪个时刻理解它。
        """
        body = await self._request(
            "/advertiser/info/",
            params={
                "advertiser_ids": json.dumps([external_id]),
                "fields": json.dumps(["balance", "currency"]),
            },
        )

        rows = _as_list(body.get("list"))
        if not rows:
            # 账户 ID 不对、或者这个 token 没有它的权限。两种都是人配错了，
            # 重试没有意义。
            raise FetchError(
                f"广告主账户 {external_id} 查不到余额：token 可能没有这个账户的权限",
                retryable=False,
            )

        row = rows[0]
        return AccountBalance(
            available=_to_decimal(row.get("balance"), field="balance"),
            # 币种缺失不兜底成某个默认值：一个币种不明的余额没法和阈值比大小，
            # 而猜错币种会让「还剩 500」在两种货币之间差出七倍。
            currency=str(row.get("currency") or "").strip().upper(),
            captured_at=datetime.now(UTC),
        )

    async def _fetch_all_pages(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """翻完所有页，把行拼起来。

        ⚠️ 翻页有硬上限（`MAX_PAGES`）：`total_page` 是平台给的，不该无条件相信
        —— 一个永不结束的任务比一个失败的任务难发现得多。
        """
        rows: list[dict[str, Any]] = []
        page = 1

        while page <= MAX_PAGES:
            body = await self._request("/report/integrated/get/", params={**params, "page": page})
            rows.extend(_flatten(row) for row in _as_list(body.get("list")))

            page_info = body.get("page_info")
            total_page = 0
            if isinstance(page_info, dict):
                total_page = _to_int(page_info.get("total_page"))
            if page >= total_page:
                return rows
            page += 1

        raise FetchError(
            f"翻页超过 {MAX_PAGES} 页仍未结束，疑似平台返回的分页信息不可信",
            retryable=False,
        )

    async def _request(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        """发一次请求，把两层失败都翻成 `FetchError`。

        🔴 **HTTP 200 不代表成功。** TikTok 把业务错误放在响应体的 `code` 里，
        HTTP 状态照样是 200 —— 只判 `response.is_success` 的话，一个「token 已
        失效」会被当成一次拿到零行的成功拉取，而那正是[设计文档第三节][design]
        要防的、最安静的那种失败。

        [design]: ../../../docs/design/2026-08-25-ads-api-fetch.md
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers={"Access-Token": self._access_token.get_secret_value()},
                )
        except httpx.HTTPError as exc:
            # 只记异常类名：httpx 的报错文本里带着完整 URL 和查询参数，而参数里
            # 有 advertiser_id（同 llm/openai_compat.py 与 notifiers/webhook.py）。
            log.warning("tiktok_request_failed", path=path, error=type(exc).__name__)
            raise FetchError(f"TikTok API 不可达：{type(exc).__name__}", retryable=True) from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.status_code >= 500:
            log.warning("tiktok_request_throttled", path=path, status_code=response.status_code)
            raise FetchError(f"TikTok API 返回 {response.status_code}", retryable=True)

        if not response.is_success:
            raise FetchError(f"TikTok API 返回 {response.status_code}", retryable=False)

        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError("TikTok API 返回的不是 JSON", retryable=True) from exc

        return _unwrap(body, path=path)

    def authorize_url(self, *, app_id: str, state: str, redirect_uri: str) -> str:
        """拼出让人去点「同意」的那个地址。

        `state` 由调用方签名并在回调时验证 —— 回调接口是**公网可打的**（浏览器
        跳转带不了 Authorization 头），`state` 是它唯一的防护，见设计文档 5.4。
        """
        request = httpx.QueryParams(
            {"app_id": app_id, "state": state, "redirect_uri": redirect_uri}
        )
        return f"{AUTHORIZE_URL}?{request}"


async def exchange_auth_code(
    *,
    app_id: str,
    app_secret: SecretStr,
    auth_code: str,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[SecretStr, tuple[str, ...]]:
    """拿回调带回来的 `auth_code` 换 access_token，返回 (token, 授权到的账户 ID)。

    做成模块级函数而不是 `TikTokProvider` 的方法：这一步**还没有 token**，而
    provider 的构造参数就是 token。塞进类里就得允许一个「没有 token 的
    provider」存在，那个状态在别的每一处都是错的。

    ⚠️ 换 token 用 **POST + JSON body**，不是 GET —— `auth_code` 和 app secret
    走查询串会被 nginx 和各级代理原样记进访问日志。
    """
    url = f"{base_url.rstrip('/')}/oauth2/access_token/"
    payload = {
        "app_id": app_id,
        "secret": app_secret.get_secret_value(),
        "auth_code": auth_code,
        "grant_type": "auth_code",
    }

    try:
        # 超时用模块常量而不是参数：ruff 的 ASYNC109 说得对 —— 一个 async 函数
        # 收 `timeout` 会让人以为它自己在计时，而这里只是把值转给 httpx。
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise FetchError(f"TikTok 授权端点不可达：{type(exc).__name__}", retryable=True) from exc

    if not response.is_success:
        raise FetchError(f"TikTok 授权端点返回 {response.status_code}", retryable=False)

    try:
        body = response.json()
    except ValueError as exc:
        raise FetchError("TikTok 授权端点返回的不是 JSON", retryable=False) from exc

    data = _unwrap(body, path="/oauth2/access_token/")
    token = str(data.get("access_token") or "")
    if not token:
        # 走到这里说明 code=0 但没有 token，只可能是协议变了。**不重试** ——
        # 授权码是一次性的，重试只会拿到「auth_code 已使用」。
        raise FetchError("TikTok 授权响应里没有 access_token", retryable=False)

    advertiser_ids = tuple(str(value) for value in _as_list(data.get("advertiser_ids")) if value)
    return SecretStr(token), advertiser_ids


def _level_spec(level: str) -> tuple[str, str, str]:
    spec = _LEVELS.get(level)
    if spec is None:
        raise FetchError(f"TikTok 适配器不支持这个层级：{level!r}", retryable=False)
    return spec


def _unwrap(body: Any, *, path: str) -> dict[str, Any]:
    """剥掉 `{code, message, data}` 这层信封，顺便把业务错误翻成 `FetchError`。"""
    if not isinstance(body, dict):
        raise FetchError("TikTok API 返回的结构不认识", retryable=True)

    code = _to_int(body.get("code"))
    if code != 0:
        # 🔴 **响应里的 message 不原样外传**：它会回显请求参数，而参数里有
        # advertiser_id。只记日志，给上层的消息自己组织。
        log.warning("tiktok_api_error", path=path, code=code)
        if code in _CREDENTIAL_CODES:
            raise FetchError(
                f"TikTok 拒绝了这次请求（错误码 {code}）：token 可能已失效或权限不足，需要重新授权",
                retryable=False,
            )
        # 兜底是**不可重试**，见 `FetchError` 的 docstring：误判成不可重试的代价
        # 是一条多余的告警，反过来的代价是数据静默停更好几天。
        raise FetchError(
            f"TikTok 拒绝了这次请求（错误码 {code}）",
            retryable=code in _RETRYABLE_CODES,
        )

    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _flatten(row: Any) -> dict[str, Any]:
    """把 `{"dimensions": {...}, "metrics": {...}}` 摊成一层。

    **键名一个字都不改**（见模块 docstring）。两个子字典的键集合天然不重叠 ——
    维度是 ID 和日期，指标是数字 —— 所以摊平不会丢东西。真撞了的话后写的赢，
    那种情况意味着平台改了协议，会在归一化时表现为字段值不合理。
    """
    if not isinstance(row, dict):
        return {}
    flat: dict[str, Any] = {}
    for section in ("dimensions", "metrics"):
        part = row.get(section)
        if isinstance(part, dict):
            flat.update(part)
    # 万一将来平台把某些字段直接放在顶层，也一并带上，但不覆盖上面两层。
    for key, value in row.items():
        if key not in ("dimensions", "metrics") and key not in flat:
            flat[key] = value
    return flat


def _group_by_day(rows: list[dict[str, Any]]) -> list[RawRows]:
    """按 `stat_time_day` 分组。日期认不出来的行**直接报错，不静默丢弃**。

    丢一行的后果是那天少一个对象的花费，而总数看起来仍然很正常 —— 这种错误
    发现不了。CSV 那边的 `skipped_rows` 是给「末尾 Total 行」用的，那是一种
    **已知且可解释**的跳过，和这里不是一回事。
    """
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        raw_day = row.get(_DIMENSION_DAY)
        day = _to_date(raw_day)
        if day is None:
            raise FetchError(
                f"响应里的 {_DIMENSION_DAY} 解析不出日期：{raw_day!r}",
                retryable=False,
            )
        grouped.setdefault(day, []).append(row)

    return [RawRows(stat_date=day, rows=grouped[day]) for day in sorted(grouped)]


def _to_date(value: Any) -> date | None:
    """`"2026-08-23 00:00:00"` → `date(2026, 8, 23)`。

    只取前 10 个字符：那个时刻部分恒为零点、且**是账户时区的零点而不是 UTC**，
    拿它去做时区换算正是这个项目最容易出错的地方（glossary「时间口径」）。
    """
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_decimal(value: Any, *, field: str) -> Decimal:
    """金额一律 `Decimal(字符串)`，**不经 float**（CLAUDE.md 硬规矩 6）。

    TikTok 的金额字段是字符串（`"9.37"`），这正好 —— 但别因为「看起来是数字」
    就用 `float()` 中转，那在 1234.5678 这种值上就开始漂。
    """
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise FetchError(f"字段 {field} 不是一个数：{value!r}", retryable=False) from exc
