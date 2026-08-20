"""worker 进程里的资源与事件循环：把同步的 Celery 接到 async 的业务代码上。

Celery 的 worker 是同步的（prefork：一个主进程 fork 出若干子进程，任务在子进程里
顺序执行），而这套代码从驱动往上全是 async。桥接本身只有一行
`loop.run_until_complete(...)`，难的是下面两个坑 —— 它们都不会当场报错，只会在
压力上来之后变成偶发故障：

## 🔴 一、连接池必须在 fork **之后**建

prefork 的子进程是 `fork()` 出来的，父进程已经建好的 socket 会被原样复制一份。
于是多个子进程共用同一条 TCP 连接，各写各的，协议流当场错乱 —— 症状是随机的
「unexpected response」「connection reset」，而且只在多进程下出现。

这里的做法是**懒建**：第一个任务执行时才建，而任务永远在子进程里执行，所以时机
天然是对的。懒建在这里不是偷懒，它就是那道保证。

## 🔴 二、事件循环必须复用同一个

asyncpg 的连接绑定在**创建它的那个事件循环**上。每个任务 `asyncio.run(...)` 会各
开一个新循环，第二个任务从池子里拿到上一个循环创建的连接，就炸在
「attached to a different loop」上 —— 而且是间歇性的：池子空的时候新建连接，恰好
能跑通。所以循环跟连接池一起，一个进程一份，存在下面这个 `_runtime` 里。

（`tests/conftest.py` 的 `live_api` 夹具踩的是同一个坑的另一面，那边的解法是不用
`TestClient` 而用 `ASGITransport`。）
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import dataclass

import structlog
from celery.signals import celeryd_init, worker_process_shutdown, worker_shutdown

from adpilot.config import get_settings
from adpilot.db import broker
from adpilot.resources import Resources, open_resources
from adpilot.tasks.app import celery_app

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Runtime:
    loop: asyncio.AbstractEventLoop
    stack: AsyncExitStack
    resources: Resources


# 进程级的一份。模块级可变状态在本仓库是要写理由的，这里的理由是它**必须**跟
# 进程绑定：连接池和事件循环都不能跨进程、跨任务重建（见模块 docstring 的两个坑）。
_runtime: _Runtime | None = None


def run[T](job: Callable[[Resources], Coroutine[None, None, T]]) -> T:
    """在本进程那个长期存在的事件循环里跑一段 async 业务代码。

    `job` 收 `Resources` 返回协程，写起来就是一行 lambda：

        runtime.run(lambda res: some_service.do(res.session_factory, ...))
    """
    runtime = _ensure_runtime()
    return runtime.loop.run_until_complete(job(runtime.resources))


def _ensure_runtime() -> _Runtime:
    global _runtime
    if _runtime is None:
        loop = asyncio.new_event_loop()
        # 也设成本线程的当前循环：库代码里调 `asyncio.get_event_loop()` 的地方
        # （不是所有第三方库都拿得到我们手上这个）才不会另开一个。
        asyncio.set_event_loop(loop)
        stack = AsyncExitStack()
        # worker 用的是自己那个 app（任务注册在它上面），不让 open_resources 再建
        # 一个 —— 一个进程里两个 Celery 实例，投递和消费就不在同一条连接上了。
        resources = loop.run_until_complete(
            stack.enter_async_context(open_resources(get_settings(), celery=celery_app))
        )
        _runtime = _Runtime(loop=loop, stack=stack, resources=resources)
        log.info("worker_runtime_started")
    return _runtime


def shutdown() -> None:
    """关掉本进程的连接池和事件循环。可重复调用。

    不关的话，worker 退出时 asyncpg 的池子还攥着连接，PostgreSQL 那侧要等到 TCP
    超时才回收 —— 频繁重启 worker 时连接数会一路涨到 `max_connections`。
    """
    global _runtime
    if _runtime is None:
        return

    runtime, _runtime = _runtime, None
    try:
        runtime.loop.run_until_complete(runtime.stack.aclose())
    finally:
        runtime.loop.close()
        log.info("worker_runtime_stopped")


def _on_startup(**_: object) -> None:
    """worker 起来时把队列拓扑建齐。

    放在 `celeryd_init`（worker 主进程、fork 之前、消费开始之前）而不是懒建：
    死信队列必须在**第一条消息被拒之前**就存在，否则那条消息直接蒸发。这里连
    出来的那条连接用完就关，不会被 fork 带进子进程。

    建不出来就让 worker 起不来 —— 一个「跑得好好的、但死信全丢」的 worker 比一个
    起不来的 worker 危险得多。
    """
    broker.declare_queues(celery_app)
    log.info("worker_queues_declared")


def _on_shutdown(**_: object) -> None:
    shutdown()


# prefork 下由子进程发 `worker_process_shutdown`；solo / threads 池没有子进程，
# 只发 `worker_shutdown`。两个都接上，`shutdown()` 本身幂等。
#
# `weak=False` 不能省：Celery 的信号默认只持弱引用，接一个没有别处引用的函数
# （比如 lambda）等于没接 —— 它会被 GC 掉，而且不会有任何报错。
celeryd_init.connect(_on_startup, weak=False)
worker_process_shutdown.connect(_on_shutdown, weak=False)
worker_shutdown.connect(_on_shutdown, weak=False)
