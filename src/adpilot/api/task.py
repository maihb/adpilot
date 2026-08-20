"""异步任务的状态查询。

只有查询，**没有投递接口**：任务是别的动作的副产物（导入完自动排一个归一化），
不是一种能被单独下单的资源。想手动重跑归一化，用 metrics 那个同步接口。
"""

from __future__ import annotations

from fastapi import APIRouter

from adpilot.api.deps import CeleryDep
from adpilot.schemas.task import TaskStatusResponse
from adpilot.services import task as task_service

router = APIRouter(tags=["tasks"])


@router.get(
    "/tasks/{task_id}",
    response_model=TaskStatusResponse,
    operation_id="getTaskStatus",
)
async def get_task_status(task_id: str, celery: CeleryDep) -> TaskStatusResponse:
    """查一个异步任务跑到哪了。

    ⚠️ **不存在的 ID 返回 200 + `PENDING`，不是 404。** Celery 的 result backend
    只在任务结束时写记录，「排队中」和「查无此 ID」对它来说是同一种状态。返回 404
    等于替它撒谎 —— 一个刚投出去还没被 worker 取走的任务会因此显示成「不存在」。

    前端轮询用 `ready` 判断结束，别比对 `state` 的字面值。
    """
    status = await task_service.get_status(celery, task_id)
    return TaskStatusResponse.model_validate(status)
