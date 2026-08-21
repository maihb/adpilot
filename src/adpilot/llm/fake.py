"""测试用的假供应商。

**CI 里一次真实的模型调用都不发**（设计文档第八节）：那会让构建依赖外部服务和一把
API key，而它验不了任何本项目自己的逻辑 —— 「那一行人话写得好不好」没有确定的正确
答案，为它写断言只会得到一个「模型换一次就红一次」的测试。

这个假实现验的是**周围那一圈**：schema 校验失败会不会重试、token 用量有没有记下来、
模型挂掉时日报还生不生成。它按次序吐出预先排好的响应，所以那三件事都能被确定性地
造出来。

放在 `src/` 而不是 `tests/` 是因为它也是**本地跑通整条链的手段**：没有 LLM 凭据
的人（clone 下来的陌生人）照样能把日报那条链走完，只是那段人话是假的。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from adpilot.llm.base import Completion, LLMError, Usage

#: 没排响应时默认吐的东西：一份合法的日报人话。让「clone 下来直接跑」有个结果。
DEFAULT_RESPONSE = (
    '{"summary": "（示例）今日花费与昨日基本持平，转化成本略有上升，'
    '未做调整，建议观察到明日。", "highlights": [], "next_steps": []}'
)


class FakeProvider:
    """按次序吐出排好的响应；排完了就一直吐最后一个（或默认那份）。

    `LLMError` 的实例会被**抛出来**而不是返回 —— 这样「供应商挂了」和「供应商乱
    答」两条路径用同一个队列就能造出来。
    """

    def __init__(
        self, responses: list[str | LLMError] | None = None, *, model: str = "fake"
    ) -> None:
        self.name = "fake"
        self.model = model
        self._queue: deque[str | LLMError] = deque(responses or [])

        #: 收到的请求，供用例断言「提示词里到底有什么」（比如客户 ID 不该出现）。
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.calls.append({"system": system, "user": user, "schema": schema})

        nxt: str | LLMError = self._queue.popleft() if self._queue else DEFAULT_RESPONSE
        if isinstance(nxt, LLMError):
            raise nxt

        return Completion(
            text=nxt,
            # 固定的 token 数：记账那条链路验的是「有没有记下来」，不是数字本身。
            usage=Usage(prompt_tokens=1200, completion_tokens=180),
            model=self.model,
        )
