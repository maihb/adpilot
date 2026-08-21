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

from adpilot.auth.token import AuthNotConfiguredError, InvalidTokenError
from adpilot.schemas.errors import ErrorResponse
from adpilot.services.exceptions import (
    ConflictError,
    DomainError,
    InvalidDataError,
    NotConfiguredError,
    NotFoundError,
    QuotaExceededError,
    ReferenceNotFoundError,
)

log = structlog.get_logger(__name__)

# 401 的响应体**固定是这一句**，不透露到底是没带 token、签名不对、过期了，还是
# 拿错了门。区分等于告诉攻击者他走到了哪一步；而对合法用户来说这几种情况的处置
# 完全一样：重新登录。理由与 `auth/token.py` 只有一个 `InvalidTokenError` 同源。
_UNAUTHORIZED_DETAIL: Final = "认证失败，请重新登录"

# 精确按类型查，**不沿继承链回退** —— 新加的异常必须自己登记一行，而不是默默
# 继承父类的状态码。继承来的码通常是错的：ReferenceNotFoundError 若跟着
# NotFoundError 走就成了 404，而它其实该是 422。
_STATUS_BY_EXCEPTION: Final[dict[type[DomainError], int]] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ReferenceNotFoundError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidDataError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    QuotaExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    NotConfiguredError: status.HTTP_503_SERVICE_UNAVAILABLE,
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


async def _handle_invalid_token(request: Request, exc: Exception) -> JSONResponse:
    """token 不可信 → 401，**响应体不说是哪一种不可信**。

    `WWW-Authenticate` 不是可有可无的礼节：没有它，401 在规范上是不完整的，
    而 HTTP 客户端（含小程序侧的封装）常据此决定要不要走重新认证的流程。
    """
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": _UNAUTHORIZED_DETAIL},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _handle_auth_not_configured(request: Request, exc: Exception) -> JSONResponse:
    """没配 `AUTH_SECRET` → 503，且**说清缺了什么**。

    这与上面那条正相反：它是部署方的问题，不是调用方的问题。回 401 会让人对着
    一个永远进不去的登录框试半天密码。这句话里没有任何敏感内容 —— 缺哪个环境
    变量，`.env.example` 里本来就写着。
    """
    log.error("auth_not_configured")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "服务端未配置 AUTH_SECRET，认证不可用"},
    )


def install_error_handlers(app: FastAPI) -> None:
    """把领域异常与认证异常的处理器挂到应用上。

    领域异常只注册基类：Starlette 查处理器时会沿异常的 MRO 往上找，所以全部子类
    都会落到那里，再由上面那张表分派具体状态码。

    认证那两个**不进 `_STATUS_BY_EXCEPTION` 那张表**：它们不是 `DomainError`
    —— `auth/` 在分层图上够不着 `services/`，两族异常从定义上就分属两层。
    """
    app.add_exception_handler(DomainError, _handle_domain_error)
    app.add_exception_handler(InvalidTokenError, _handle_invalid_token)
    app.add_exception_handler(AuthNotConfiguredError, _handle_auth_not_configured)


def responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """给接口声明错误响应，让它们出现在 OpenAPI 里。

    不声明的话，异常处理器返回的 4xx 在 schema 里根本不存在，前端生成出来的
    客户端会以为这个接口只可能成功 —— 而错误分支恰恰是最需要类型的地方。
    """
    described = {
        status.HTTP_401_UNAUTHORIZED: "没带 token、token 无效或已过期",
        status.HTTP_404_NOT_FOUND: "资源不存在",
        status.HTTP_409_CONFLICT: "与已有数据冲突",
        status.HTTP_413_CONTENT_TOO_LARGE: "上传的文件超过大小上限",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "入参不合法，或引用了不存在的对象",
        status.HTTP_429_TOO_MANY_REQUESTS: "超过了本地设置的用量上限",
        status.HTTP_503_SERVICE_UNAVAILABLE: "服务端缺少必要配置（认证、LLM 等）",
    }
    return {code: {"model": ErrorResponse, "description": described[code]} for code in codes}
