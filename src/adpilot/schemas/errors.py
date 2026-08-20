"""错误响应的形状。

与 FastAPI 自带的 `HTTPException` 和 422 校验错误保持同一个字段名（`detail`），
前端才能用一条分支处理所有失败 —— 形状不统一的话，每加一个错误码就要改一次
错误处理代码。
"""

from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    detail: str
