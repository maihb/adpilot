"""异步任务的出参。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TaskStatusResponse(BaseModel):
    """一个异步任务此刻的状态。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: str

    #: Celery 的状态名：`PENDING` / `STARTED` / `RETRY` / `SUCCESS` / `FAILURE`。
    #: ⚠️ **`PENDING` 同时意味着「排队中」和「查无此 ID」** —— result backend 只在
    #: 任务结束时才写记录，这两种情况在它眼里长得一模一样。所以别拿它当「任务存在」
    #: 的判据，也别在前端把它显示成「正在处理」以外的意思。
    state: str

    #: 跑完了没有（成功或失败都算跑完）。前端轮询的停止条件用它，不要自己比对
    #: 状态名 —— 状态名是 Celery 的枚举，会随版本增减。
    ready: bool

    #: 任务的返回值，只有成功时才有。形状由具体任务决定（归一化任务给的是
    #: `{account_id, days, rows, snapshots, skipped_rows}`）。
    result: dict[str, object] | None = None

    #: 失败时的**异常类名**，不是原始报错。完整原因在 worker 日志里，被判定为
    #: 不该重试的消息在 `adpilot.dead` 死信队列里躺着。只给类名的理由和就绪探针
    #: 一样：驱动的异常消息里可能带着 DSN 或主机名。
    error: str | None = None
