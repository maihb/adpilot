"""「结构化输入 → 结构化输出」，以及失败时的重试与记账明细。

这是 `llm/` 对外的主入口。它做四件事：把契约对象和 JSON Schema 拼成提示词、调
供应商、把回来的文本解析成 Pydantic 对象、校验不过就再问一次。

**它不落库** —— 这一层够不着 `db`（模块 docstring 讲了为什么这是好事）。所以每次
调用的记账明细装在 `CallRecord` 里返回给 `services/`，由那一层写进 `llm_calls`。
**失败时也返回**（挂在异常上）：一次失败的调用同样烧了 token，不记账就等于账目
里凭空少一笔。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from adpilot.llm.base import (
    CallStatus,
    Completion,
    LLMError,
    LLMInvalidOutputError,
    LLMProvider,
)
from adpilot.llm.prompts import Prompt, Purpose

log = structlog.get_logger(__name__)

#: 默认试几次。**1 次重试就够**：schema 校验失败通常是模型这次跑偏了，再问一次
#: 多半就对了；连着两次都不合格说明是提示词或 schema 本身的问题，而那种问题重试
#: 一百次也解决不了，只会烧钱。
DEFAULT_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class CallRecord:
    """一次调用（含重试）的记账明细，`services/` 拿它落 `llm_calls`。

    🔴 **不含请求正文，也不含响应正文。** 提示词里有客户的花费数字，而这张表是
    给人查成本和排障用的，谁都可能把它导出来贴到别处。要排查具体内容时，靠
    `purpose` + `prompt_version` 找回当时那份提示词（它在代码里），而不是靠库里
    留一份副本。
    """

    provider: str
    model: str
    purpose: Purpose
    prompt_version: str
    status: CallStatus

    #: 实际发出去几次请求。>1 就意味着有过一次不合格的输出。
    attempts: int

    #: 供应商不给 usage 时是 `None`（不是 0，见 `base.Usage`）。
    prompt_tokens: int | None
    completion_tokens: int | None

    #: 失败时的异常类名。**只记类名不记消息** —— 消息里可能带着 URL 或响应正文。
    error_type: str | None

    #: 这次调用总共花了多久（含重试）。慢到几十秒时日报任务会跟着卡住，而那件事
    #: 只有记了时长才看得见。
    duration_ms: int


@dataclass(frozen=True, slots=True)
class StructuredResult[T: BaseModel]:
    """校验通过的输出，以及它的记账明细。"""

    output: T
    record: CallRecord


class LLMGenerationError(LLMError):
    """重试用尽仍然没拿到合格输出。

    `record` 挂在异常上，是因为**失败也要记账**：那几次请求一样烧了 token。
    调用方的写法是 `except LLMGenerationError as exc: await _record(exc.record)`。
    """

    def __init__(self, record: CallRecord, message: str) -> None:
        super().__init__(message)
        self.record = record


async def generate[T: BaseModel](
    provider: LLMProvider,
    *,
    prompt: Prompt,
    payload: BaseModel,
    output_type: type[T],
    attempts: int = DEFAULT_ATTEMPTS,
) -> StructuredResult[T]:
    """把 `payload` 交给模型，要一个 `output_type` 回来。

    **只重试「答得不合格」，不重试「答不上来」。** 网络抖动、超时、5xx 在这里
    直接抛：日报和诊断都跑在 Celery 任务里，那一层已经有退避重试策略，在这里再叠
    一层只会让一次外网抖动变成几分钟的阻塞（同 `notifiers/webhook.py` 那条规矩）。

    重试时会把上一次的校验错误附在提示词后面。多这三行的理由是它明显提高第二次的
    成功率 —— 模型看不到自己错在哪的话，往往会原样再错一遍。
    """
    schema = output_type.model_json_schema()
    user = _user_message(payload, schema)

    started = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        hint = "" if last_error is None else _retry_hint(last_error)
        try:
            completion = await provider.complete(
                system=prompt.system,
                user=user + hint,
                schema=schema,
            )
        except LLMError as exc:
            # 供应商侧的失败：不重试，但要把记账明细带上去。
            raise LLMGenerationError(
                _record(provider, prompt, CallStatus.UNAVAILABLE, attempt, None, exc, started),
                f"{prompt.name} 调用失败：{type(exc).__name__}",
            ) from exc

        try:
            output = _parse(completion, output_type)
        except LLMInvalidOutputError as exc:
            last_error = exc
            log.warning(
                "llm_output_rejected",
                purpose=prompt.name,
                attempt=attempt,
                # 只记异常类名与 Pydantic 的错误位置，不记模型吐了什么
                error=type(exc).__name__,
            )
            continue

        return StructuredResult(
            output=output,
            record=_record(
                provider,
                prompt,
                CallStatus.OK,
                attempt,
                completion,
                None,
                started,
            ),
        )

    raise LLMGenerationError(
        _record(provider, prompt, CallStatus.INVALID_OUTPUT, attempts, None, last_error, started),
        f"{prompt.name} 连续 {attempts} 次输出都过不了契约校验",
    )


def _user_message(payload: BaseModel, schema: dict[str, Any]) -> str:
    """事实 + 目标形状。

    **schema 既进这段文本、也传给供应商**（`complete(schema=...)`），不是冗余：
    支持严格 structured output 的端点用参数那一份，而只支持 `json_object` 的端点
    （本地 Ollama、一部分代理）只能靠文本里这一份。少了任何一份，都会有一类端点
    退化成「自由发挥」。
    """
    facts = payload.model_dump_json(indent=2, exclude_none=True)
    return (
        f"下面是全部事实：\n{facts}\n\n"
        f"按这个 JSON Schema 输出一个 JSON 对象：\n{json.dumps(schema, ensure_ascii=False)}"
    )


def _retry_hint(error: Exception) -> str:
    return f"\n\n上一次的输出没能通过校验，原因：{error}。请重新输出一个符合 Schema 的 JSON 对象。"


def _parse[T: BaseModel](completion: Completion, output_type: type[T]) -> T:
    """文本 → 契约对象。不合格一律抛 `LLMInvalidOutputError`。"""
    text = _strip_code_fence(completion.text)
    try:
        return output_type.model_validate_json(text)
    except ValidationError as exc:
        # 只带 Pydantic 的错误摘要（哪个字段、缺什么），不带模型吐出来的正文 ——
        # 那里面有客户数据，而这条消息会进重试提示词和日志。
        raise LLMInvalidOutputError(f"{exc.error_count()} 个字段不符合契约") from exc


def _strip_code_fence(text: str) -> str:
    """剥掉 ```json 围栏。

    提示词里已经明说了不要围栏，但模型时不时还是会加 —— 为这件事重试一次是白花
    钱，而剥掉它是三行确定性代码。**只剥围栏，不做任何别的修补**：真的不是 JSON
    时就该走校验失败那条路，在这里手工纠错等于让一个安静的解析器去猜模型的意图。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    without_open = stripped.split("\n", 1)[-1]
    return without_open.rsplit("```", 1)[0].strip()


def _record(
    provider: LLMProvider,
    prompt: Prompt,
    status: CallStatus,
    attempts: int,
    completion: Completion | None,
    error: Exception | None,
    started: float,
) -> CallRecord:
    return CallRecord(
        provider=provider.name,
        # 记供应商实际用的那个模型名（网关会做别名映射），成本是按它算的
        model=completion.model if completion is not None else provider.model,
        purpose=prompt.name,
        prompt_version=prompt.version,
        status=status,
        attempts=attempts,
        prompt_tokens=completion.usage.prompt_tokens if completion is not None else None,
        completion_tokens=completion.usage.completion_tokens if completion is not None else None,
        error_type=type(error).__name__ if error is not None else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
