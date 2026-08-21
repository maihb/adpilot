"""LLM 调用的唯一出口：配置检查、每日闸门、记账。

`llm/` 那一层够不着数据库（它的模块 docstring 讲了为什么这是好事），所以「调用」
和「记账」被拆成了两半：那边负责把结构化输入变成结构化输出，这边负责在前面拦一道
额度、在后面把账记上。**业务代码不要直接 import `llm.structured`** —— 绕过这里就
等于绕过了记账和闸门。

## 为什么失败不抛异常，而是返回一个 output 为 None 的结果

因为**失败也要记账**，而记账是一条 INSERT：让异常冒到 `api/` 层的话，
`session_scope` 会回滚整个事务，那条刚写下的账就跟着没了 —— 于是「烧了 token 却
查不到记录」，正是这张表最不该有的漏洞。

返回值形态还顺带把「LLM 挂掉时日报照样生成」变成了显式分支：调用方拿到
`output is None` 就走人话留空那条路，而不是靠记得写 try/except。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings
from adpilot.llm.base import CallStatus, LLMProvider
from adpilot.llm.openai_compat import OpenAICompatProvider
from adpilot.llm.prompts import Prompt
from adpilot.llm.structured import CallRecord, LLMGenerationError, generate
from adpilot.models.llm_call import LLMCall
from adpilot.services.exceptions import NotConfiguredError, QuotaExceededError

log = structlog.get_logger(__name__)

#: 单价的计价单位：每**百万** token。供应商的报价页都是这个口径。
TOKENS_PER_PRICE_UNIT: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Outcome[T: BaseModel]:
    """一次调用的结果。

    `output is None` 表示没拿到合格输出（供应商挂了，或者连着几次都过不了契约
    校验）。**这不是异常情况** —— 日报的数字部分是确定性的，不该被模型的可用性
    绑架，所以调用方该做的是把人话字段留空并标注「未生成」。
    """

    output: T | None

    #: 已经写进 `llm_calls` 的那一行（还没提交，跟着调用方的事务走）。日报要记
    #: 「这段话是哪次调用生成的」，靠它的 `id`。
    call: LLMCall


def create_provider(settings: Settings) -> LLMProvider:
    """按配置造一个供应商。没配就抛 `NotConfiguredError`（→ 503）。

    **调用方通常应该先问 `settings.llm_is_configured`**：没配 LLM 是正常状态，
    日报那条链会走「人话留空」的分支，而不是把 503 抛给运营。会走到这个异常的
    只有「明确要求做一次 LLM 调用」的入口（比如按需诊断）。
    """
    if not settings.llm_is_configured:
        raise NotConfiguredError("服务端未配置 LLM_BASE_URL / LLM_MODEL，无法调用模型")

    return OpenAICompatProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )


async def run[T: BaseModel](
    session: AsyncSession,
    settings: Settings,
    *,
    prompt: Prompt,
    payload: BaseModel,
    output_type: type[T],
    account_id: int | None = None,
    provider: LLMProvider | None = None,
) -> Outcome[T]:
    """调一次模型，把账记上。

    `provider` 留了口子是为了测试注入假实现（`llm/fake.py`）—— **CI 里一次真实
    调用都不发**（设计文档第八节）。不传就按配置造一个。
    """
    active = provider if provider is not None else create_provider(settings)
    await _ensure_daily_quota(session, settings)

    try:
        result = await generate(active, prompt=prompt, payload=payload, output_type=output_type)
    except LLMGenerationError as exc:
        call = await _record_call(session, exc.record, settings, account_id)
        log.warning(
            "llm_call_failed",
            purpose=prompt.name.value,
            status=call.status.value,
            attempts=call.attempts,
            error=call.error_type,
        )
        return Outcome(output=None, call=call)

    call = await _record_call(session, result.record, settings, account_id)
    log.info(
        "llm_call_finished",
        purpose=prompt.name.value,
        prompt_version=prompt.version,
        attempts=call.attempts,
        duration_ms=call.duration_ms,
    )
    return Outcome(output=result.output, call=call)


async def calls_today(session: AsyncSession) -> int:
    """今天已经调了几次（**UTC 自然日**）。

    用 UTC 而不是某个账户的时区：这个计数不属于任何账户（诊断属于告警、日报属于
    账户、将来还会有别的），而额度是**整个实例**的一道闸门。挑一个账户的时区去截
    只会让「今天」的含义取决于哪个账户先跑。
    """
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(select(func.count(LLMCall.id)).where(LLMCall.created_at >= since))
    return total or 0


def estimate_cost(
    settings: Settings,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> Decimal | None:
    """预估这次花了多少钱。**算不出来就返回 `None`，不返回 0。**

    两种算不出来：没配单价，以及供应商没回 `usage`（本地 vLLM、部分代理会省掉）。
    两种都记 NULL —— 0 的意思是「免费」，而实情是「不知道」。混起来会让月度成本
    统计悄悄少一截，而少了的那部分看起来完全正常（同 `days_left` 那条 null 口径）。

    token 数只缺一个也返回 `None`：只算已知的那一半会给出一个**系统性偏低**的
    数字，而偏低的成本估算正是最不该出现的错误方向。
    """
    if not settings.llm_prices_are_configured:
        return None
    if prompt_tokens is None or completion_tokens is None:
        return None

    return (
        Decimal(prompt_tokens) * settings.llm_input_cost_per_mtok
        + Decimal(completion_tokens) * settings.llm_output_cost_per_mtok
    ) / TOKENS_PER_PRICE_UNIT


async def _ensure_daily_quota(session: AsyncSession, settings: Settings) -> None:
    """撞上每日上限就拒绝，不继续花钱。

    ⚠️ **并发下可能略微超出**：计数和写入之间没有锁。这是刻意的取舍 —— 为一道
    「防失控」的闸门去加行锁，代价（争用、死锁面）远大于「某天多调了两次」。它要
    挡的是写错的循环，不是精确计费。
    """
    used = await calls_today(session)
    if used < settings.llm_daily_call_limit:
        return

    log.error("llm_daily_quota_exceeded", used=used, limit=settings.llm_daily_call_limit)
    raise QuotaExceededError(
        f"今天的 LLM 调用已达上限（{used}/{settings.llm_daily_call_limit}），"
        "调整 LLM_DAILY_CALL_LIMIT 或等明天"
    )


async def _record_call(
    session: AsyncSession,
    record: CallRecord,
    settings: Settings,
    account_id: int | None,
) -> LLMCall:
    """把一次调用（成功或失败）落进 `llm_calls`。"""
    call = LLMCall(
        account_id=account_id,
        purpose=record.purpose,
        provider=record.provider,
        model=record.model,
        prompt_version=record.prompt_version,
        status=record.status,
        attempts=record.attempts,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        estimated_cost=estimate_cost(
            settings,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
        ),
        error_type=record.error_type,
        duration_ms=record.duration_ms,
    )
    session.add(call)
    await session.flush()
    return call


def status_is_ok(call: LLMCall) -> bool:
    """这次调用成没成。收口在这里，调用方不去比对字符串。"""
    return call.status is CallStatus.OK
