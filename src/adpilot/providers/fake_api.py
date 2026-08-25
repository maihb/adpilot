"""假的拉取型 provider：不联网，产出确定性的假数据。

## 它解决的是一个日程问题，不是测试问题

TikTok 的 API 权限要审核，几个工作日到数周（[设计文档第二节][design]）。而
「自动拉取」这条链路有七个环节 —— 凭据解密、调用、落快照、排归一化、写日指标、
写余额、失败告警 —— **等审核结果再来第一次验证它们，等于把七个环节的 bug 攒到
同一天暴露**。

有了它，审核那几天里整条链路是可以跑通、可以看见、可以修的。等真凭据到位，换的
只有 provider 名字。

[design]: ../../../docs/design/2026-08-25-ads-api-fetch.md

## 🔴 两道防线，防的是同一件事：假数据被当成真的

1. **`ENVIRONMENT=prod` 下注册表不给造它**（`registry.py`）。
2. **对象名一律 `demo-` 前缀、客户名 `示例｜`** —— 与 `seed.py` 同一套约定，
   有测试盯着（`tests/test_seed.py`）。看板上一眼能认出来。

数字本身也是**刻意不真实**的：花费固定在个位数、转化数为零。一份「看起来很像
真实投放」的假数据，才是最危险的东西。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from adpilot.providers.base import AccountBalance, FetchError, ParseResult, RawRows

#: 假对象的名字前缀，与 `seed.py` 同一套约定。
DEMO_PREFIX = "demo-"

#: 每天造几个对象。两个就够验证「按对象 upsert」这件事，多了只是让看板变吵。
_OBJECTS_PER_DAY = 2


class FakeFetchProvider:
    """按 (账户, 日期) 确定性地编数：同样的入参永远得到同样的行。

    **确定性不是为了好看，是为了让 upsert 可验证** —— 同一天拉两次，
    `daily_metrics` 必须还是那几行、数字不变。随机数会让「重复拉取是否幂等」
    这个问题每次得到不同的答案。
    """

    name = "fake_api"

    def __init__(self, *, fail: bool = False) -> None:
        #: 让它按需失败。**这是它第二重要的用途**：设计文档第三节那条「拉不到
        #: 数必须开告警」，只有在能稳定复现一次失败时才验得了。
        self._fail = fail

    async def fetch(
        self,
        *,
        external_id: str,
        level: str,
        since: date,
        until: date,
    ) -> ParseResult:
        if self._fail:
            raise FetchError("假 provider 被要求失败", retryable=False)

        days: list[RawRows] = []
        day = since
        while day <= until:
            days.append(RawRows(stat_date=day, rows=_rows_for(day, level=level)))
            day += timedelta(days=1)
        return ParseResult(days=days, skipped_rows=0)

    async def fetch_balance(self, *, external_id: str) -> AccountBalance:
        if self._fail:
            raise FetchError("假 provider 被要求失败", retryable=False)
        return AccountBalance(
            available=Decimal("123.45"),
            currency="USD",
            captured_at=datetime.now(UTC),
        )


def _rows_for(day: date, *, level: str) -> list[dict[str, Any]]:
    """造一天的行。

    键名照搬 TikTok API 的形态（`stat_time_day`、`campaign_id`、`spend`），
    这样它走的就是**和真 provider 完全相同的那条归一化路径** —— 一个用自己
    专属字段名的假 provider 验不出任何东西。
    """
    id_field, name_field = _fields_for(level)
    return [
        {
            id_field: f"{DEMO_PREFIX}{level}-{index}",
            name_field: f"{DEMO_PREFIX}{level}-{index}",
            "stat_time_day": f"{day.isoformat()} 00:00:00",
            # 花费随日期小幅摆动，但永远是个位数 —— 看板上能看出「有变化」，
            # 又不会被误认成真实投放。
            "spend": f"{3 + day.day % 5}.{20 + index}",
            "impressions": str(100 + day.day * 3 + index),
            "clicks": str(2 + day.day % 4 + index),
            # 转化恒为 0：一份假数据不该让任何人对着它算 CPA。
            "conversion": "0",
        }
        for index in range(_OBJECTS_PER_DAY)
    ]


def _fields_for(level: str) -> tuple[str, str]:
    if level == "ad":
        return "ad_id", "ad_name"
    if level == "adgroup":
        return "adgroup_id", "adgroup_name"
    if level == "account":
        return "advertiser_id", "advertiser_name"
    return "campaign_id", "campaign_name"
