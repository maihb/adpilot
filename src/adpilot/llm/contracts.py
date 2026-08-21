"""LLM 层自己的输入输出契约。

**为什么不复用 `schemas/`**：分层契约里 `llm` 在 `schemas` 之下，import 不到。
这看起来像重复，其实是好事 —— LLM 层的契约和对外 API 的契约本来就该分开演进，
中间那道翻译由 `services/` 显式地做（那一层读得懂、review 得了），而不是把 ORM
对象直接扔给模型。

## 🔴 输出侧一个数字字段都没有

这是[设计文档第五节](../../../docs/design/2026-08-19-mvp-design.md)第 2、3 条边界
的落地形态：日报里的**数字全部由代码从 `daily_metrics` 算**，LLM 只写那一行人话。
输出模型里只有 `str` 和 `list[str]`，所以「模型把 CPA 编成另一个值」在结构上不可
能发生 —— 它填不进一个不存在的字段。

⚠️ 拦不住的是它**在散文里**编一个百分比。那道防线是人工修订，不是这份契约。

## 输入侧的数字为什么也是字符串

`metrics` 里的值已经是格式化好的字符串（`"1,234.56 USD"`、`"+24.1%"`）。两个理由：
喂给模型的本来就是文本，提前格式化能让「12.3400」这种尾巴不出现在日报里；以及
`Decimal` 一旦经过 JSON 序列化就有变成浮点的风险，而这条链路上没有任何一步需要
再算一次 —— **算数在 `services/`，这里只负责措辞**。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MetricLine(BaseModel):
    """一个已经算好的指标。模型**只读它，不产生它**。"""

    #: 中文标签，直接进日报（「花费」「CPA」「转化数」）。
    label: str

    #: 格式化好的值，含单位或币种。
    value: str

    #: 与对照期相比怎么变的（「较上周同日 +24.1%」）。算不出来时留空 —— 留空
    #: 的意思是「没有可比数据」，模型的提示词里说明了不许对空值下结论。
    change: str | None = None


class ActionLine(BaseModel):
    """一次投放调整。`reason` 是这里最值钱的部分（见 `docs/business/actions.md`）。"""

    performed_at: str
    summary: str
    reason: str


class DailyReportInput(BaseModel):
    """写一份日报所需的全部事实。**模型看不到这之外的任何东西**。

    没有账户 ID、客户 ID、token、内部主键 —— 不是怕模型看懂，是这些东西会随提示词
    一起发到第三方的服务器上，而它们对措辞没有任何帮助。
    """

    account_name: str
    stat_date: str

    #: 口径必须写进日报（conventions.md 的时间一节）：客户拿他自己后台的数字来对，
    #: 不注明时区就永远差一截，而那是解释不清的差异、不是数据错误。
    timezone: str
    currency: str

    metrics: list[MetricLine] = Field(max_length=20)

    #: 规则算出来的告警人话（`alerts.message`）。**已经是结论**，模型只负责解释
    #: 它、不负责判断成不成立。
    alerts: list[str] = Field(default_factory=list, max_length=20)

    #: 本期做了什么。空着是**正常的输入**（那天什么都没做），但空着的日报**发不
    #: 出去** —— 那道校验在 `services/`，不在这里。
    actions: list[ActionLine] = Field(default_factory=list, max_length=50)


class DailyReportNarrative(BaseModel):
    """日报里那一行人话。**只有文字字段，这是刻意的**（见模块 docstring）。

    多余字段按 Pydantic 默认忽略掉，不设 `extra="forbid"`：模型爱多吐一个
    `"confidence"` 之类的键，为此重试一次是白烧钱，而多出来的字段没有任何东西会
    去读它。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    #: 一段总述：今天整体怎么样、变化的可能原因。日报的价值就在这一段。
    summary: str = Field(min_length=1, max_length=800)

    #: 值得单独点出来的几件事，一条一句。
    highlights: list[str] = Field(default_factory=list, max_length=5)

    #: 🔴 **建议，永远只是建议。** 这个系统不碰广告平台的写接口（设计文档第五节
    #: 第 1 条），所以这里写的是「观察到周一再看」「建议换素材而不是降价」这类
    #: 方向，不是一条会被谁执行的指令。
    next_steps: list[str] = Field(default_factory=list, max_length=5)


class DiagnosisInput(BaseModel):
    """诊断一条告警要的事实。**规则已经判定它成立了**，模型只解释原因。"""

    account_name: str

    #: 告警种类与它的人话摘要，两者都由规则产生。
    alert_kind: str
    alert_message: str

    #: 相关的历史数字，格式化好的字符串（「近 7 天频次：2.1 → 3.2」）。
    context: list[MetricLine] = Field(default_factory=list, max_length=20)

    #: 最近的调整。很多异动的原因就在这里，不给的话模型只能猜。
    actions: list[ActionLine] = Field(default_factory=list, max_length=20)


class Diagnosis(BaseModel):
    """一条告警的解释。同样只有文字字段。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    #: 最可能的原因，按可能性从高到低。
    likely_causes: list[str] = Field(min_length=1, max_length=5)

    #: 人接下来该去核实什么（「看一眼频次是不是过 3 了」）。
    suggested_checks: list[str] = Field(default_factory=list, max_length=5)

    #: 一句话结论式的建议。**不含任何会被自动执行的东西** —— 没有「把预算改成 X」
    #: 这种字段，那是设计文档第五节第 1 条那条硬边界的形态。
    suggestion: str = Field(min_length=1, max_length=400)
