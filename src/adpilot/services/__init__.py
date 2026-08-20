"""业务逻辑。**这一层不认识 HTTP。**

不返回状态码、不碰 `Request`、不抛 `HTTPException`，只抛
[`exceptions`](exceptions.py) 里的领域异常，由 `api/` 翻译成状态码。

理由不是洁癖：D6 起 Celery 任务和规则巡检会调用同一批服务函数，那些调用方
没有请求对象可给。完整的分层契约见
[`docs/code-rules/architecture.md`](../../../docs/code-rules/architecture.md)。
"""
