"""邀请码的出入参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: 生成时能指定的有效期范围（天）。上限见 `services/invite.py` 的 `MAX_TTL` ——
#: 不给「永不过期」这个选项是刻意的。
MIN_TTL_DAYS = 1
MAX_TTL_DAYS = 365
DEFAULT_TTL_DAYS = 30


class InviteCreateRequest(BaseModel):
    ttl_days: int = Field(
        default=DEFAULT_TTL_DAYS,
        ge=MIN_TTL_DAYS,
        le=MAX_TTL_DAYS,
        description="有效期天数，默认 30 天",
    )


class InviteResponse(BaseModel):
    """邀请码的状态。**不含码本身** —— 库里存的是哈希，还原不回来。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    use_count: int
    created_at: datetime


class InviteCreatedResponse(InviteResponse):
    """刚生成出来的邀请码，**这是明文码唯一一次出现的地方**。

    单独一个模型而不是给 `InviteResponse` 加一个可选字段：可选字段会让「列表里
    要不要带上码」变成一个每次都要重新判断的问题，而答案永远是不带。
    """

    code: str


class InviteListResponse(BaseModel):
    items: list[InviteResponse]
    total: int
