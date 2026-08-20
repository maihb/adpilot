"""应用入口。

本地跑：

    uv run uvicorn adpilot.main:app --reload --reload-dir src
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from adpilot.api import (
    ad_account,
    alert,
    balance,
    client,
    daily_metric,
    health,
    imports,
    task,
)
from adpilot.api.errors import install_error_handlers
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
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    install_error_handlers(app)

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(client.router, prefix=settings.api_prefix)
    app.include_router(ad_account.router, prefix=settings.api_prefix)
    app.include_router(imports.router, prefix=settings.api_prefix)
    app.include_router(daily_metric.router, prefix=settings.api_prefix)
    app.include_router(balance.router, prefix=settings.api_prefix)
    app.include_router(alert.router, prefix=settings.api_prefix)
    app.include_router(task.router, prefix=settings.api_prefix)
    return app


app = create_app()
