"""LLM 层：重试、记账、以及那两条「只能靠盯代码形状」的门禁。

**这里一次真实的模型调用都不发**（设计文档第八节）。理由不是省钱：那会让构建依赖
外部服务和一把 API key，而它验不了任何本项目自己的逻辑 —— 「那一行人话写得好不
好」没有确定的正确答案，为它写断言只会得到一个「模型换一次就红一次」的测试。

所以验的是**周围那一圈**：校验失败会不会重试、token 用量和成本有没有落库、模型
挂掉时调用方拿到的是不是「没有输出」而不是异常。另外两条扫源码的门禁见文件末尾。
"""

from __future__ import annotations

import hashlib
import types
import typing
from collections.abc import Sequence
from decimal import Decimal

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Environment, Settings
from adpilot.llm import prompts
from adpilot.llm.base import CallStatus, LLMUnavailableError
from adpilot.llm.contracts import (
    DailyReportInput,
    DailyReportNarrative,
    Diagnosis,
    DiagnosisInput,
    MetricLine,
)
from adpilot.llm.fake import FakeProvider
from adpilot.llm.prompts import Purpose
from adpilot.llm.structured import LLMGenerationError, generate
from adpilot.models.llm_call import LLMCall
from adpilot.services import llm as llm_service
from adpilot.services.exceptions import NotConfiguredError, QuotaExceededError

# 一份合法的日报人话，和一份缺了必填字段的。
_GOOD = '{"summary": "花费与昨日持平，转化成本略升。", "highlights": [], "next_steps": []}'
_BAD = '{"highlights": ["缺了 summary"]}'


def _input() -> DailyReportInput:
    return DailyReportInput(
        account_name="示例｜账户",
        stat_date="2026-08-20",
        timezone="America/Anchorage",
        currency="USD",
        metrics=[MetricLine(label="花费", value="1,234.56 USD", change="较上周同日 +24.1%")],
    )


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "environment": Environment.TEST,
        "llm_base_url": "http://llm.invalid/v1",
        "llm_model": "test-model",
        "llm_api_key": SecretStr("not-a-real-key"),
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]  # 动态构造，字段名在上面


# --- 重试 -------------------------------------------------------------------


async def test_invalid_output_is_retried_once_and_then_succeeds() -> None:
    """第一次答得不合格 → 再问一次 → 拿到合格的。

    这是 LLM 这条链路上**最常见**的失败，不是异常情况：模型偶尔跑偏，再问一次
    多半就对了。所以它不该冒到调用方那里去。
    """
    provider = FakeProvider([_BAD, _GOOD])

    result = await generate(
        provider,
        prompt=prompts.DAILY_REPORT,
        payload=_input(),
        output_type=DailyReportNarrative,
    )

    assert result.output.summary.startswith("花费与昨日持平")
    assert result.record.attempts == 2
    assert result.record.status is CallStatus.OK


async def test_retry_tells_the_model_what_was_wrong() -> None:
    """重试时把上次的校验错误附上去 —— 看不到自己错在哪的模型会原样再错一遍。"""
    provider = FakeProvider([_BAD, _GOOD])

    await generate(
        provider,
        prompt=prompts.DAILY_REPORT,
        payload=_input(),
        output_type=DailyReportNarrative,
    )

    assert "没能通过校验" in provider.calls[1]["user"]
    assert "没能通过校验" not in provider.calls[0]["user"]


async def test_repeated_invalid_output_raises_with_a_billable_record() -> None:
    """连着两次都不合格就抛 —— 但**记账明细要挂在异常上**。

    那两次请求一样烧了 token。不带着它，账目里就会凭空少一笔，而少的恰好是「模型
    表现最差」的那些次。
    """
    provider = FakeProvider([_BAD, _BAD])

    with pytest.raises(LLMGenerationError) as raised:
        await generate(
            provider,
            prompt=prompts.DAILY_REPORT,
            payload=_input(),
            output_type=DailyReportNarrative,
        )

    assert raised.value.record.status is CallStatus.INVALID_OUTPUT
    assert raised.value.record.attempts == 2


async def test_unavailable_provider_is_not_retried() -> None:
    """供应商挂了**不重试**：日报跑在 Celery 任务里，退避重试是那一层的事。

    在这里叠一层只会让一次外网抖动变成几分钟的阻塞（同 notifiers/webhook.py）。
    """
    provider = FakeProvider([LLMUnavailableError("端点不可达"), _GOOD])

    with pytest.raises(LLMGenerationError) as raised:
        await generate(
            provider,
            prompt=prompts.DAILY_REPORT,
            payload=_input(),
            output_type=DailyReportNarrative,
        )

    assert raised.value.record.status is CallStatus.UNAVAILABLE
    assert raised.value.record.attempts == 1
    # 队列里那条合法响应没被取走 —— 证明确实只发了一次
    assert len(provider.calls) == 1


async def test_code_fence_is_stripped() -> None:
    """模型时不时会加 ```json 围栏，为这个重试一次是白花钱。"""
    provider = FakeProvider([f"```json\n{_GOOD}\n```"])

    result = await generate(
        provider,
        prompt=prompts.DAILY_REPORT,
        payload=_input(),
        output_type=DailyReportNarrative,
    )

    assert result.record.attempts == 1


# --- 成本 -------------------------------------------------------------------


def test_cost_is_none_when_prices_are_not_configured() -> None:
    """没配单价 → NULL（「不知道」），**不是 0**（「免费」）。"""
    cost = llm_service.estimate_cost(_settings(), prompt_tokens=1000, completion_tokens=500)

    assert cost is None


def test_cost_is_none_when_the_provider_omits_usage() -> None:
    """本地 vLLM 和部分代理不回 usage。只算已知的那一半会系统性低估。"""
    settings = _settings(llm_input_cost_per_mtok=Decimal("0.15"))

    assert llm_service.estimate_cost(settings, prompt_tokens=None, completion_tokens=500) is None
    assert llm_service.estimate_cost(settings, prompt_tokens=1000, completion_tokens=None) is None


def test_cost_uses_decimal_all_the_way() -> None:
    """单价是「每百万 token」。一次日报几分钱，浮点在这个量级上会开始漂。"""
    settings = _settings(
        llm_input_cost_per_mtok=Decimal("0.15"),
        llm_output_cost_per_mtok=Decimal("0.60"),
    )

    cost = llm_service.estimate_cost(settings, prompt_tokens=1200, completion_tokens=180)

    # 1200 * 0.15 / 1e6 + 180 * 0.60 / 1e6
    assert cost == Decimal("0.000288")


# --- 服务层：记账与闸门 -----------------------------------------------------


@pytest.mark.integration
async def test_successful_call_is_billed(live_session: AsyncSession) -> None:
    """**token 用量和成本要落库。** 这张表要能回答「这个月花了多少」。"""
    settings = _settings(
        llm_input_cost_per_mtok=Decimal("0.15"),
        llm_output_cost_per_mtok=Decimal("0.60"),
    )

    outcome = await llm_service.run(
        live_session,
        settings,
        prompt=prompts.DAILY_REPORT,
        payload=_input(),
        output_type=DailyReportNarrative,
        provider=FakeProvider([_GOOD]),
    )

    assert outcome.output is not None
    assert outcome.call.id is not None
    assert outcome.call.status is CallStatus.OK
    assert outcome.call.purpose is Purpose.DAILY_REPORT
    # 🔴 提示词版本号：没有它，三个月后没法把这份日报和当时那版口径对上
    assert outcome.call.prompt_version == prompts.DAILY_REPORT.version
    assert outcome.call.prompt_tokens == 1200
    assert outcome.call.completion_tokens == 180
    assert outcome.call.estimated_cost == Decimal("0.000288")


@pytest.mark.integration
async def test_failed_call_is_billed_too(live_session: AsyncSession) -> None:
    """🔴 失败也要记账，而且调用方拿到的是「没有输出」不是异常。

    两件事在这一条里一起验：失败的那次一样烧了 token（不记就等于账目里凭空少一
    笔）；以及 `run` 不抛 —— 抛的话 `session_scope` 会回滚，刚写下的账跟着没了。
    """
    outcome = await llm_service.run(
        live_session,
        _settings(),
        prompt=prompts.DIAGNOSIS,
        payload=DiagnosisInput(
            account_name="示例｜账户",
            alert_kind="metric_anomaly",
            alert_message="cpa 较上周同日上升 42.0%",
        ),
        output_type=Diagnosis,
        provider=FakeProvider([LLMUnavailableError("端点不可达")]),
    )

    assert outcome.output is None
    assert outcome.call.status is CallStatus.UNAVAILABLE
    # 只记异常类名，不记消息 —— 消息里可能带 URL 或响应正文
    assert outcome.call.error_type == "LLMUnavailableError"
    assert outcome.call.prompt_tokens is None


@pytest.mark.integration
async def test_daily_quota_blocks_further_calls(live_session: AsyncSession) -> None:
    """撞上上限就拒绝，不继续花钱。

    防的是一个写错的循环在夜里把额度跑光 —— 自托管意味着花的是使用者自己的钱。
    """
    settings = _settings(llm_daily_call_limit=1)

    first = await llm_service.run(
        live_session,
        settings,
        prompt=prompts.DAILY_REPORT,
        payload=_input(),
        output_type=DailyReportNarrative,
        provider=FakeProvider([_GOOD]),
    )
    assert first.output is not None

    with pytest.raises(QuotaExceededError):
        await llm_service.run(
            live_session,
            settings,
            prompt=prompts.DAILY_REPORT,
            payload=_input(),
            output_type=DailyReportNarrative,
            provider=FakeProvider([_GOOD]),
        )

    # 被拒的那次**不落账**：它根本没发出去
    total = await live_session.scalar(select(func.count(LLMCall.id)))
    assert total == 1


def test_missing_configuration_is_a_503_not_a_400() -> None:
    """没配 LLM 是部署方的问题，不是调用方的问题。"""
    with pytest.raises(NotConfiguredError):
        llm_service.create_provider(_settings(llm_base_url="", llm_model=""))


# --- 两条扫源码的门禁 -------------------------------------------------------
#
# 下面这两条没有可观察的行为差异，测不出来，只能盯代码形状（同
# tests/test_auth_token.py 最后那条的套路）。

# 标成 Sequence 而不是 list：pydantic 的 mypy 插件给每个模型生成精确的 __init__，
# 于是 `type[子类]` 对不上不变容器里的 `type[BaseModel]`。Sequence 是协变的。
_TEXT_ONLY_OUTPUTS: Sequence[type[BaseModel]] = [DailyReportNarrative, Diagnosis]


@pytest.mark.parametrize("model", _TEXT_ONLY_OUTPUTS, ids=lambda m: m.__name__)
def test_llm_output_has_no_numeric_fields(model: type[BaseModel]) -> None:
    """🔴 **LLM 的输出契约里一个数字字段都不许有。**

    这是设计文档第五节第 2、3 条边界的落地形态：日报里的数字全部由代码从
    `daily_metrics` 算，模型只写那一行人话。加一个 `cpa: Decimal` 进来，「模型把
    CPA 编成另一个值」就从「结构上不可能」变成了「但愿它别」——而那一天不会有
    任何东西报错。

    （拦不住它在散文里编一个百分比。那道防线是人工修订，见 llm/__init__.py。）
    """
    for name, field in model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)
        # list[str] 和 `str | None` 都要拆开看里面那层，别的类型原样比对
        unwrap = origin in (list, types.UnionType)
        inner = typing.get_args(annotation) if unwrap else (annotation,)

        assert all(arg is str or arg is type(None) for arg in inner), (
            f"{model.__name__}.{name} 的类型是 {annotation}；"
            "LLM 的输出契约里只能有文字字段，数字一律由代码从 daily_metrics 算"
        )


#: 提示词正文的指纹。**改了正文就要改版本号，然后把新指纹填回这里。**
#: 它不判提示词写得好不好（那没有确定的正确答案），只判「改了却没升版本」。
_PROMPT_FINGERPRINTS = {
    Purpose.DAILY_REPORT: ("v1", "98026a2ab21f092c"),
    Purpose.DIAGNOSIS: ("v1", "591db5f8c036c4f1"),
}


@pytest.mark.parametrize("purpose", list(Purpose), ids=lambda p: p.value)
def test_prompt_changes_come_with_a_version_bump(purpose: Purpose) -> None:
    """🔴 提示词改一次，日报的口径就变一次。

    没有版本号的话，三个月后没有任何办法把「这份日报」和「当时那版提示词」对上
    —— 而那是评估这套系统值不值得信的唯一依据。版本号跟着每次调用落进
    `llm_calls.prompt_version`。
    """
    prompt = prompts.ALL[purpose]
    expected_version, expected_digest = _PROMPT_FINGERPRINTS[purpose]
    digest = hashlib.sha256(prompt.system.encode()).hexdigest()[:16]

    assert prompt.version == expected_version, (
        f"{purpose.value} 的版本号变成了 {prompt.version}；"
        "确认正文也改了，然后把这里的版本号和指纹一起更新"
    )
    assert digest == expected_digest, (
        f"{purpose.value} 的提示词正文改了（指纹 {digest}），但版本号还是 "
        f"{prompt.version}。**先把版本号加一**，再把新指纹填回 _PROMPT_FINGERPRINTS"
    )
