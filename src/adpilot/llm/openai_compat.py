"""OpenAI 兼容端点的适配器。**MVP 只有这一个真实实现。**

## 为什么不做 Claude / Gemini 的原生适配器

主设计文档原本要给它们各写一个「作为适配器注册表的活证明」，D13 设计时收缩掉了
（[LLM 日报与诊断设计](../../../docs/design/2026-08-21-llm-reports.md)第六节）：
那两家**都提供 OpenAI 兼容端点**，原生适配器的增量只是它们特有的功能（prompt
caching、thinking 之类），而写日报用不到；「注册表设计能用」这个证明 `providers/`
已经给出了 —— 那里真有多个实现。为同一个设计再写两个用不上的实现，是为了证明而
写代码。

`LLMProvider` 协议照样留着，这是**接口预留不是省略**。换供应商改三个环境变量：
DeepSeek、Kimi、通义、本地 Ollama、vLLM 全都兼容。

## 为什么不进 `Resources`

[architecture.md](../../../docs/code-rules/architecture.md) 那条「加一个外部系统要
动五处」说的是**连接池型**依赖（PG / Mongo / Redis / RabbitMQ）。LLM 不是：一天
调几次，每次新建一个 `AsyncClient` 的开销完全可以忽略，而进了 `Resources` 就意味
着就绪探针要去探它 —— **那是一次真花钱的请求**，还会让「服务健不健康」取决于第
三方的可用性。出站通知（`notifiers/webhook.py`）出于同一个理由也不在那里。
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import structlog
from pydantic import SecretStr

from adpilot.llm.base import Completion, LLMUnavailableError, Usage

log = structlog.get_logger(__name__)

#: 默认超时。比 webhook 那 3 秒宽得多：日报是异步任务里生成的，没有人在等这个
#: 响应，而一份日报的推理本来就要十几秒。宽到 60 秒仍然是**上限**不是期望值 ——
#: 没有它的话，一个卡住的端点会把 worker 的一个槽位永久占住。
TIMEOUT_SECONDS: Final = 60.0

#: 输出长度上限。契约里 `summary` 最多 800 字符、加上几条列表，2000 token 绰绰
#: 有余。给上限是为了防跑飞：模型偶尔会陷进复读，而那是按 token 计费的。
MAX_OUTPUT_TOKENS: Final = 2000

#: 采样温度压得低。日报不需要创造力，需要的是同样的输入给出稳定的措辞 —— 每天
#: 换一种腔调会让客户以为口径变了。
TEMPERATURE: Final = 0.3


class OpenAICompatProvider:
    """任何讲 OpenAI `/chat/completions` 协议的服务。

    构造参数收零件而不是 `Settings`：这一层不认识应用配置（分层契约里 `config`
    在它之下够得着，但收整个 `Settings` 会让适配器跟着配置一起演进）。取值那一步
    在 `services/llm.py`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.name = "openai_compat"
        self.model = model
        # 末尾斜杠有没有都行 —— 使用者从供应商文档里拷过来的 base_url 两种都有，
        # 而拼出 `//chat/completions` 的症状是 404，跟斜杠八竿子打不着。
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._timeout = timeout

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        """发一次请求，回一段没解析的文本。

        **`response_format` 只用 `json_object`，不用 `json_schema` 严格模式。**
        后者是 OpenAI 自家的扩展，兼容端点支持得参差不齐 —— 用了它，换一家供应商
        可能直接 400，而那会把「不绑定任何一家」这个决定作废掉。目标形状靠提示词
        里那份 Schema 传达，出来的东西一律过 Pydantic 校验（`structured.py`），
        所以这里退一步不损失正确性，只是多一次偶尔的重试。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": TEMPERATURE,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                )
        except httpx.HTTPError as exc:
            # 🔴 只记异常类名：httpx 的报错文本里带着完整 URL，而 base_url 有时
            # 自带鉴权路径。同 notifiers/webhook.py。
            log.warning("llm_request_failed", error=type(exc).__name__)
            raise LLMUnavailableError(f"LLM 端点不可达：{type(exc).__name__}") from exc

        if not response.is_success:
            # 状态码可以记，响应正文不行 —— 供应商的错误体里会原样回显请求内容，
            # 而请求内容里有客户的花费数字。
            log.warning("llm_request_rejected", status_code=response.status_code)
            raise LLMUnavailableError(f"LLM 端点返回 {response.status_code}")

        return _to_completion(response.json(), fallback_model=self.model)


def _to_completion(body: Any, *, fallback_model: str) -> Completion:
    """把响应体摊成 `Completion`。形状不对一律算「供应商没答上来」。

    不把它算成「输出不合格」是有意的：`structured.py` 会为后者重试，而一个不讲这
    个协议的端点重试一百次也还是不讲。
    """
    if not isinstance(body, dict):
        raise LLMUnavailableError("LLM 端点返回的不是 JSON 对象")

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMUnavailableError("LLM 端点没有返回任何 choice")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise LLMUnavailableError("LLM 端点返回的 choice 里没有文本内容")

    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    model = body.get("model")

    return Completion(
        text=content,
        usage=Usage(
            # 缺 usage 的端点（本地 vLLM、部分代理）记 None 而不是 0：0 的意思是
            # 「没花」，实情是「不知道花了多少」。
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
        ),
        model=model if isinstance(model, str) else fallback_model,
    )


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
