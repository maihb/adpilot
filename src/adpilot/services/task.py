"""异步任务的投递与状态查询。

**和 `adpilot.tasks/` 是两回事，别混**：那边是任务体（在 worker 里跑的代码），
这边是**生产者侧** —— 把任务放进队列、回头问它跑成没跑成。接口进程只碰这一边。

投递一律走 `send_task` 按名字发，不 import 任务函数。两个理由：

* 接口进程因此不需要装任务那一侧的任何依赖，也不会因为 import 任务模块而把 worker
  的初始化代码（事件循环、连接池）拖进 Web 进程；
* 生产者和消费者之间只剩「任务名 + 参数」这一个契约，两边可以分开部署、分开重启。

代价是**参数名写错不会有编译期报错**，会变成 worker 那边的一次失败。所以任务名是
常量（`db/broker.py`），参数在下面这几个函数里收口，调用方不自己拼 kwargs。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import structlog
from celery import Celery
from celery.result import AsyncResult

from adpilot.db import broker
from adpilot.db.broker import MAIN_QUEUE, TASK_NORMALIZE_ACCOUNT

log = structlog.get_logger(__name__)

# 🔴 投递失败要**快速**放弃。
#
# Celery 默认拿的是连接池里那条连接，而池子在建连接时会一路重试到
# `broker_connection_max_retries`（默认 100 次，间隔还递增）。实测 broker 不可达时
# 一次 `send_task` 卡了 **19 秒** —— 而调用它的是导入接口，那就是让人对着转圈等
# 19 秒，然后得到一个「其实已经成功了」的响应。
#
# 所以这里自己建连接、自己定重试上限，再把它交给 `send_task`。`timeout` 是整个
# 重试循环的总预算（kombu 的语义），所以这是一道**硬上限**：最坏 2 秒。够覆盖
# 「RabbitMQ 正在重启」这种一两秒的空窗，又不会把请求拖垮。
#
# **worker 那一侧不受影响**：它的重连走 Celery 自己的 consumer 循环，本来就该
# 一直重试下去。改 `broker_connection_max_retries` 会把那边一起改掉，所以没那么做。
_CONNECT_POLICY: dict[str, object] = {
    "max_retries": 2,
    "timeout": broker.BROKER_CONNECT_TIMEOUT_SECONDS,
    "interval_start": 0,
    "interval_step": 0.3,
    "interval_max": 0.3,
}


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """一个任务此刻的状态。"""

    task_id: str
    state: str
    ready: bool
    result: dict[str, object] | None
    error: str | None


async def enqueue_normalize(
    celery: Celery,
    *,
    account_id: int,
    stat_date: date | None = None,
) -> str | None:
    """把归一化任务投进队列，返回任务 ID；投不进去返回 `None`。

    🔴 **投递失败不抛异常。** 调用它的是导入接口，而那时快照**已经落进 Mongo 了**
    —— 让整个请求以 500 收场会让人以为导入没成功，于是再导一次，于是多一条快照。
    归一化随时能重跑（接口上就有一个同步的入口），所以这里的正确行为是记一条日志、
    把 `task_id` 留空，让调用方看得出「排队这一步没成」。
    """
    return await _send(
        celery,
        TASK_NORMALIZE_ACCOUNT,
        {"account_id": account_id, "stat_date": stat_date.isoformat() if stat_date else None},
    )


async def get_status(celery: Celery, task_id: str) -> TaskStatus:
    """查一个任务的状态。

    ⚠️ **查不到的 ID 也返回 `PENDING`，不是错误。** Celery 的 result backend 只在
    任务**结束**时写一条记录，排队中和「这个 ID 根本不存在」在它眼里长得一模一样。
    所以这个接口不返回 404 —— 那会是撒谎。

    失败时只给**异常类名**，不给原始报错：驱动的异常消息里可能带着 DSN 或主机名。
    完整原因看 worker 日志，被判定为不该重试的那些消息在 `adpilot.dead` 队列里躺着。
    """
    return await asyncio.to_thread(_read_status, celery, task_id)


def _read_status(celery: Celery, task_id: str) -> TaskStatus:
    """同步地读一次任务状态 —— result backend 的客户端是同步的，所以走线程池。"""
    async_result: AsyncResult[object] = celery.AsyncResult(task_id)
    state = str(async_result.state)
    payload = async_result.result

    if async_result.successful() and isinstance(payload, dict):
        return TaskStatus(task_id=task_id, state=state, ready=True, result=payload, error=None)
    if async_result.failed():
        return TaskStatus(
            task_id=task_id,
            state=state,
            ready=True,
            result=None,
            error=type(payload).__name__,
        )
    return TaskStatus(task_id=task_id, state=state, ready=False, result=None, error=None)


async def _send(celery: Celery, name: str, kwargs: dict[str, object]) -> str | None:
    """投递一条消息。`send_task` 是同步阻塞的（kombu 没有 async 接口），走线程池。"""
    try:
        task_id = await asyncio.to_thread(_publish, celery, name, kwargs)
    except Exception:
        # 连不上 broker、或者队列参数对不上。只记类名和任务名，别把 broker URL
        # 带出来 —— 它里面有密码。
        log.exception("task_enqueue_failed", task=name)
        return None

    log.info("task_enqueued", task=name, task_id=task_id)
    return task_id


def _publish(celery: Celery, name: str, kwargs: dict[str, object]) -> str:
    # 每次投递现开一条连接（而不是用 Celery 的连接池），换来的是上面那道硬上限。
    # 代价是一次 AMQP 握手 —— 对「一天几次、人点出来的」导入完全不值一提。
    with celery.connection_for_write() as connection:
        connection.ensure_connection(**_CONNECT_POLICY)  # type: ignore[arg-type]  # kombu 的存根把这几个参数写成了位置化的具体类型
        result = celery.send_task(name, kwargs=kwargs, queue=MAIN_QUEUE, connection=connection)
    return str(result.id)
