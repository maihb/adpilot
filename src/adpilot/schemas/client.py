"""客户的出入参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# 与 `models/client.py` 的列宽保持一致。写在这里是为了让超长输入在入口就被
# Pydantic 拦成 422，而不是一路走到数据库、由 asyncpg 抛一个 500。
NAME_MAX_LENGTH = 128


class ClientCreateRequest(BaseModel):
    """建一个客户。

    `name` 唯一：文件导入那条链路要靠它幂等找回客户（一份 CSV 里通常只有客户名、
    没有 ID）。所以重名返回 409，而不是默默建出第二行。
    """

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    note: str | None = None


class ClientUpdateRequest(BaseModel):
    """改客户，只动传上来的字段。

    **停止合作置 `is_active=False`，不删行** —— 历史日报和结算记录都挂在客户
    下面，删了它们就成了孤儿。所以这个领域没有 DELETE 接口。
    """

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    note: str | None = None
    is_active: bool | None = None


class ClientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    note: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ClientListResponse(BaseModel):
    """列表出参的固定形状：`items` + `total`。

    `total` 是**过滤后的总数**，不是本页条数 —— 前端要靠它算总页数。
    """

    items: list[ClientResponse]
    total: int
