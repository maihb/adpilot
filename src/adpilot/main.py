"""应用入口。

本地跑：

    uv run uvicorn adpilot.main:app --reload --reload-dir src
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, Depends, FastAPI, status

from adpilot.api import (
    ad_account,
    alert,
    auth,
    balance,
    client,
    daily_metric,
    health,
    imports,
    invite,
    task,
)
from adpilot.api.deps import require_operator
from adpilot.api.errors import install_error_handlers, responses
from adpilot.config import Settings, get_settings
from adpilot.logging import configure_logging
from adpilot.resources import open_resources

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    做成工厂而不是模块级单例，是为了让测试能用自己的配置建应用，不必去动
    运行测试的那个进程的环境变量。
    """
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        async with open_resources(settings) as resources:
            app.state.resources = resources
            log.info("startup_complete", environment=settings.environment)
            yield
            log.info("shutdown_started")

    app = FastAPI(
        title="adpilot",
        description=(
            "Self-hosted ad performance hub for Meta and TikTok Ads: pulls "
            "spend and conversion data, keeps raw snapshots for audit, and "
            "writes daily reports you can hand to a client."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # 生产环境关掉交互文档和 schema。**不是指望靠它藏住什么** —— 仓库是公开
        # 的，接口清单本来就在源码里。关掉的是一个免认证、能一次性枚举出全部路由
        # 与出入参形状的入口：那是给扫描器省事的东西，而它对使用者的价值在生产上
        # 恰好是零。前端的类型生成读的是本地或 CI 起的非生产实例，不受影响。
        docs_url=None if settings.is_production else "/docs",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    install_error_handlers(app)

    # 健康探针**不认证**：探针的调用方是 Docker 和负载均衡，它们没地方放 token；
    # 而一个需要凭据才能回答「你还活着吗」的探针，在凭据配错时会把「配置问题」
    # 表现成「服务挂了」。就绪探针只上报依赖名和异常类名，不含可利用的细节
    # （理由在 api/health.py 的 `_run_probe` 里）。
    app.include_router(health.router, prefix=settings.api_prefix)
    # 换 token 的入口自己不能要 token。
    app.include_router(auth.router, prefix=settings.api_prefix)

    # 其余全部要运营身份。**统一在这里挂，不逐个 handler 写** —— 写在 handler 上
    # 的话，漏掉一个不会有任何报错，只会变成一条谁都能调的接口。这里漏掉一个则会
    # 被 tests/test_auth_guard.py 当场拦下：那条测试遍历 openapi.json，要求每个
    # 接口要么带 security、要么在一份显式的豁免清单里。
    for router in (
        client.router,
        invite.router,
        ad_account.router,
        imports.router,
        daily_metric.router,
        balance.router,
        alert.router,
        task.router,
    ):
        _include_operator_router(app, router, settings)
    return app


def _include_operator_router(app: FastAPI, router: APIRouter, settings: Settings) -> None:
    """挂一组只有运营能调的路由。

    `responses` 里补 401 是为了让它出现在 OpenAPI 里 —— 不声明的话，生成出来的
    前端客户端会以为这些接口不可能返回 401，而那恰恰是每个请求都要处理的分支。
    """
    app.include_router(
        router,
        prefix=settings.api_prefix,
        dependencies=[Depends(require_operator)],
        responses=responses(status.HTTP_401_UNAUTHORIZED),
    )


app = create_app()
