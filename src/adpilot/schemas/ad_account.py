"""广告账户的出入参。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from adpilot.models.ad_account import Platform

EXTERNAL_ID_MAX_LENGTH = 64
NAME_MAX_LENGTH = 128


def _known_timezone(value: str) -> str:
    """挡住拼错的时区名。

    🔴 **这个校验不是形式主义。** `timezone` 是 `stat_date` 的口径依据
    （[glossary](../../../docs/business/glossary.md) 的「时间口径」一节），写错
    一个字母，这个账户所有日期的日切点就是错的 —— 而数据看起来**完全正常**，
    只有客户拿自己后台的数字来对时才会发现差一截，那时已经积累了几周的错数据。

    在入口挡住比事后回填便宜得多，何况回填还要重跑归一化。
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"不是有效的 IANA 时区名：{value!r}") from exc
    return value


# IANA 时区名（`America/Anchorage` 这种），不是 UTC 偏移量 —— 偏移量在夏令时
# 切换那天是错的，而广告数据恰恰按自然日切。
TimezoneName = Annotated[str, Field(max_length=64), AfterValidator(_known_timezone)]

# ISO 4217 三字母代码。大写是规范写法，小写一律拒掉而不是悄悄转换：同一个账户
# 在库里存 "usd" 和 "USD" 两种写法，跨账户汇总时按币种分组就会分裂成两组。
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class AdAccountCreateRequest(BaseModel):
    """建一个广告账户。

    `(platform, external_id)` 唯一：导入是按这一对找回账户的，没有这条约束，
    重复建一个同名账户会让同一天的数据分裂到两行。所以重复返回 409。
    """

    client_id: int
    platform: Platform
    external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    currency: CurrencyCode
    timezone: TimezoneName


class AdAccountUpdateRequest(BaseModel):
    """改账户，只动传上来的字段。

    **`platform` 与 `external_id` 不在这里** —— 它们是账户的身份，改了等于换了
    一个账户，而历史 `daily_metrics` 仍挂在原 `account_id` 上，改完那些数据就
    对不上平台了。真要换，建一个新账户、把旧的停用。

    `currency` 和 `timezone` 可改（平台侧确实会改），但注意**已归一化的历史数据
    不会跟着变** —— 那些行记的是当时的口径。改口径要重跑归一化。
    """

    client_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    currency: CurrencyCode | None = None
    timezone: TimezoneName | None = None
    is_active: bool | None = None


class AdAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    platform: Platform
    external_id: str
    name: str
    currency: str
    timezone: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AdAccountListResponse(BaseModel):
    items: list[AdAccountResponse]
    total: int
