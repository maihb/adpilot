"""供应商协议，以及一次调用的原始产物。

这个模块**不 import 任何 adpilot 内部模块**，和 `providers/base.py` 是同一条规矩：
适配器只认外部协议，不认业务。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class LLMError(Exception):
    """LLM 这条链路上的失败。

    分两个子类，因为**调用方的处置不同**：不可用是「等会儿再来」，输出不合格是
    「再问一次」。合成一个的话，一次网络抖动和一个满嘴跑火车的模型会走同一条
    重试路径，而后者重试再多次也没用。
    """


class LLMUnavailableError(LLMError):
    """供应商这次没能给出回答：网络不通、超时、5xx、认证失败、限流。

    🔴 **message 里不许带响应正文。** 提示词里有客户的花费数字，而错误消息是最
    容易被顺手贴进 issue 的东西。只记异常类名和状态码 —— 同
    `notifiers/webhook.py` 的那条规矩。
    """


class LLMInvalidOutputError(LLMError):
    """回来的东西过不了契约校验：不是 JSON，或者字段对不上。

    这是**正常会发生**的事，不是异常情况 —— 所以 `structured.py` 会重试，重试
    仍失败才把它抛出去。
    """


class CallStatus(StrEnum):
    """一次调用的结局。三种，因为**处置各不相同**。

    定义在这里而不是 `structured.py`：`models/llm_call.py` 要拿它当列类型，而那一层
    不该认识调用与重试的逻辑，只该认识「结局有哪几种」。
    """

    OK = "ok"

    #: 供应商答了，但答的东西过不了契约校验（重试用尽之后）。看到这个去看提示词。
    INVALID_OUTPUT = "invalid_output"

    #: 供应商没能答：网络、超时、5xx、认证、限流。看到这个去看部署和额度。
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Usage:
    """这次调用烧了多少 token。

    两个字段都可空：**不是所有兼容端点都回 `usage`**（本地跑的 vLLM、某些代理层
    会省掉它）。缺了就记 `None` 而不是 0 —— 0 的意思是「没花」，而这里的实情是
    「不知道花了多少」，把两者混起来会让月度成本统计悄悄少一截。
    """

    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True, slots=True)
class Completion:
    """一次调用回来的原始产物。

    `text` 是**没解析过的**模型输出。解析和校验是 `structured.py` 的事，放在这里
    会让每个适配器都自己实现一遍重试。
    """

    text: str
    usage: Usage

    #: 供应商实际用的模型名。可能与请求的不同（网关会做别名映射），而记账要记
    #: **真正跑的那个** —— 单价是按它算的。
    model: str


class LLMProvider(Protocol):
    """把「一段提示词 + 一份 JSON Schema」变成「一段 JSON 文本」。

    **只有这一个方法**。流式输出、函数调用、多轮对话都不在协议里 —— 日报是异步
    生成的，没有人盯着屏幕等它一个字一个字出来（设计文档第九节）。

    实现方要负责的只有两件事：把请求翻译成自家协议，以及把失败翻译成上面那两个
    异常。**不要在实现里重试** —— 重试次数是业务决定，写在 `structured.py`。
    """

    #: 落进 `llm_calls.provider` 的值。定了就不要动，它是历史记录的来源标记。
    name: str

    #: 请求时用的模型名，供记账与提示词版本对照。
    model: str

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion: ...
