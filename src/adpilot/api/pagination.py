"""分页入参。

约定见 [`docs/code-rules/api.md`](../../../docs/code-rules/api.md#分页)：`page`
从 1 起，`page_size` 默认 20、**硬上限 100**，出参固定 `items` + `total`。

上限写成 `le=` 让 Pydantic 自动 422，而不是在 handler 里悄悄截断成 100 ——
截断的话客户端要了 500 条、拿到 100 条却看不出被截过，会把「后面没有了」当成
数据到头了。

定义放在 `api/` 而不是 `schemas/`，是因为 `Query` 是 HTTP 层的东西：`schemas/`
在分层图里位于 `services/` 之下，让它 import fastapi 会把整个业务层拖上 HTTP 依赖。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

PageParam = Annotated[int, Query(ge=1, description="页码，从 1 起")]
PageSizeParam = Annotated[
    int,
    Query(ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}"),
]
