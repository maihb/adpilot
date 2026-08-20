"""Celery 任务：异步链路的入口。

这一层与 `api/` 是**平级的两个入口**，共用下面的 `services/`：接口那条链路由 HTTP
请求驱动，这条由 RabbitMQ 里的消息驱动。所以

* 任务体只做**编排**（解参数、调服务、把结果整理成能进 result backend 的形状），
  业务判断一律留在 `services/`；
* 任务函数的参数必须是 **JSON 可序列化的原始类型**（`date` 传 ISO 字符串），
  它们要经过 broker；
* `tasks/` 不 import `api/`，`api/` 也不 import `tasks/` —— 分层契约里两者同层，
  互相 import 就是违约。接口投递任务走的是**按名字发**
  （`resources.celery.send_task`，名字常量在 `db/broker.py`）。

跑起来：

    uv run celery -A adpilot.tasks.app worker --loglevel=info
"""
