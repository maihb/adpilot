"""领域异常 → HTTP 状态码。

**这是 `services/` 与 HTTP 之间唯一的翻译点。** 业务层只抛领域异常，状态码的
决定全部集中在下面那张表里 —— 分散到各个 handler 的话，同一种失败在两个接口
上返回不同的码，而这种不一致要等前端报 bug 才会被发现。

加一个领域异常就要来这里补一行。漏了不会静默：`_handle_domain_error` 会记一条
`unmapped_domain_error` 日志再按 500 走。
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from adpilot.schemas.errors import ErrorResponse
from adpilot.services.exceptions import (
    ConflictError,
    DomainError,
    NotFoundError,
    ReferenceNotFoundError,
)

log = structlog.get_logger(__name__)

# 精确按类型查，**不沿继承链回退** —— 新加的异常必须自己登记一行，而不是默默
# 继承父类的状态码。继承来的码通常是错的：ReferenceNotFoundError 若跟着
# NotFoundError 走就成了 404，而它其实该是 422。
_STATUS_BY_EXCEPTION: Final[dict[type[DomainError], int]] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ReferenceNotFoundError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """把领域异常渲染成与 FastAPI 自带错误同形的响应体。"""
    if not isinstance(exc, DomainError):  # 注册时绑的就是 DomainError，理论上到不了
        raise exc

    status_code = _STATUS_BY_EXCEPTION.get(type(exc))
    if status_code is None:
        log.error("unmapped_domain_error", error_type=type(exc).__name__)
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    return JSONResponse(status_code=status_code, content={"detail": exc.message})


def install_error_handlers(app: FastAPI) -> None:
    """把领域异常的处理器挂到应用上。

    只注册基类：Starlette 查处理器时会沿异常的 MRO 往上找，所以全部子类都会
    落到这里，再由上面那张表分派具体状态码。
    """
    app.add_exception_handler(DomainError, _handle_domain_error)


def responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """给接口声明错误响应，让它们出现在 OpenAPI 里。

    不声明的话，异常处理器返回的 4xx 在 schema 里根本不存在，前端生成出来的
    客户端会以为这个接口只可能成功 —— 而错误分支恰恰是最需要类型的地方。
    """
    described = {
        status.HTTP_404_NOT_FOUND: "资源不存在",
        status.HTTP_409_CONFLICT: "与已有数据冲突",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "入参不合法，或引用了不存在的对象",
    }
    return {code: {"model": ErrorResponse, "description": described[code]} for code in codes}
