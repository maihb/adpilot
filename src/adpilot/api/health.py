"""存活探针与就绪探针。

拆成两个是有意义的：**存活（liveness）** 回答「这个进程是不是卡死了，要不要重启」，
它绝不能依赖任何外部服务 —— 否则数据库抖一下就会触发重启风暴，把一次能自愈的
小故障放大成事故。**就绪（readiness）** 回答「这个实例现在能不能接流量」，
依赖检查正该放在这里。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from adpilot.api.deps import ResourcesDep
from adpilot.resources import Resources

router = APIRouter(tags=["health"])
log = structlog.get_logger(__name__)

# 超过这个时间还答不上来的依赖，一律按「挂了」处理。探针必须快速返回：
# 对等待方来说，一个很慢的「正常」和「不正常」没有区别。
PROBE_TIMEOUT_SECONDS = 2.0


class DependencyStatus(BaseModel):
    """单个后端服务的探测结果。"""

    name: str
    healthy: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    dependencies: list[DependencyStatus]


class LivenessResponse(BaseModel):
    status: Literal["alive"]


@router.get("/health/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    """报告进程还活着。这里刻意什么都不检查。"""
    return LivenessResponse(status="alive")


async def _probe_postgres(resources: Resources) -> DependencyStatus:
    async with resources.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return DependencyStatus(name="postgres", healthy=True)


async def _probe_mongo(resources: Resources) -> DependencyStatus:
    await resources.mongo_client.admin.command("ping")
    return DependencyStatus(name="mongodb", healthy=True)


async def _probe_redis(resources: Resources) -> DependencyStatus:
    await resources.redis.ping()
    return DependencyStatus(name="redis", healthy=True)


async def _run_probe(
    name: str,
    coro: Coroutine[Any, Any, DependencyStatus],
) -> DependencyStatus:
    """等待一个探针，把任何失败都转成结构化结果。

    探针绝不能抛异常：一个依赖连不上，应该表现为一条 unhealthy 记录，而不是把
    整个就绪响应带崩。驱动异常只上报异常类名 —— 驱动的报错信息里可能带着 DSN 或
    主机名，而这个接口往往不需要认证就能访问。
    """
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return DependencyStatus(
            name=name,
            healthy=False,
            detail=f"timed out after {PROBE_TIMEOUT_SECONDS:g}s",
        )
    except Exception as exc:
        log.warning("readiness_probe_failed", dependency=name, error=str(exc))
        return DependencyStatus(name=name, healthy=False, detail=type(exc).__name__)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    """并发探测所有后端服务并汇总结果。

    只要有一个依赖不可用就返回 503，让负载均衡把这个实例摘出去，而不是继续把
    注定失败的流量送进来。
    """
    dependencies = await asyncio.gather(
        _run_probe("postgres", _probe_postgres(resources)),
        _run_probe("mongodb", _probe_mongo(resources)),
        _run_probe("redis", _probe_redis(resources)),
    )

    all_healthy = all(dep.healthy for dep in dependencies)
    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if all_healthy else "degraded",
        dependencies=list(dependencies),
    )
