"""worker 的入口模块：`celery -A adpilot.tasks.app`。

Celery 的命令行要按模块路径找到一个 app 实例，所以这里**必须**有一个模块级单例
—— 这是本仓库「不加包级全局变量、配置一律显式传参」那条约定的唯一例外，例外的
理由就是 worker 进程的启动方式由 Celery 定，不由我们定。

接口进程不 import 这个模块：它拿的是 `Resources.celery`（`resources.py` 里建的
另一个实例），投递走 `send_task` 按名字发。两边共享的只有 `db/broker.py` 里的
队列与任务名常量。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from celery import Task
from celery.exceptions import Reject

from adpilot.config import get_settings
from adpilot.db.broker import create_celery_app

if TYPE_CHECKING:
    # celery 运行时的 `Task` **不是**泛型（`Task[...]` 当场抛 TypeError），而
    # celery-types 的存根把它标成了 `Task[ParamSpec, Return]`。基类是在运行时求值
    # 的，`from __future__ import annotations` 帮不上忙，所以两边只能这样分开写。
    _TaskBase = Task[..., object]
else:
    _TaskBase = Task

# 任务模块得让 worker import 到才会注册。写成字符串交给 Celery 的 `include`，
# 而不是在本文件底部 import 它们 —— 那会绕成环（任务模块要从这里取 `celery_app`）。
# **加一个任务模块就要在这里补一行**，漏了的症状是队列里的消息被 worker 以
# 「Received unregistered task」拒掉，而任务代码本身看起来毫无问题。
TASK_MODULES: Final = ("adpilot.tasks.normalize", "adpilot.tasks.alerts")

celery_app = create_celery_app(get_settings(), include=TASK_MODULES)


class AdpilotTask(_TaskBase):
    """全部任务的基类：重试策略与失败去向都定在这里。

    分成三种结局，对应三种不同的原因：

    1. **瞬时故障**（数据库正在重启、网络抖动）→ 指数退避重试。这是默认结局：
       `autoretry_for` 收所有异常，因为「哪些异常算瞬时」这个清单永远列不全，
       而对一个确定性的失败多试五次的代价，远小于漏掉一次本可自愈的故障。
    2. **数据本身不对**（快照里缺必需列、账户不存在）→ 任务体把领域异常转成
       `Reject(requeue=False)`，消息直接进死信队列。重试一万次它还是不对，
       而重试会把真正的原因埋在一堆一模一样的报错里。
    3. **重试用尽** → 任务失败，状态和 traceback 留在 result backend 里。
       ⚠️ **这一类不进死信队列** —— 想知道有没有这种失败，查任务状态（
       `GET /api/tasks/{task_id}`）或看 worker 日志，别只盯着 `adpilot.dead`。

    `Reject` 必须列进 `dont_autoretry_for`：它也是 `Exception` 的子类，不排除的话
    会被上面第 1 条捞回去重试，第 2 条就等于没写。
    """

    max_retries = 5
    autoretry_for: tuple[type[Exception], ...] = (Exception,)
    dont_autoretry_for: tuple[type[Exception], ...] = (Reject,)
    # 退避从 2 秒起翻倍，封顶 5 分钟，带 full jitter。**抖动不是锦上添花**：
    # 一次数据库重启会让所有在途任务同时失败、同时重试，没有抖动的话它们会踩着
    # 同一个节拍一起回来，刚起来的数据库再被打趴一次。
    retry_backoff = 2
    retry_backoff_max = 300
    retry_jitter = True


__all__ = ["AdpilotTask", "celery_app"]
