"""日报的出入参。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from adpilot.models.report import ReportStatus
from adpilot.schemas.daily_metric import DerivedMetrics, divide


class ReportNarrative(BaseModel):
    """日报里那段人话：一段总述 + 几条要点 + 几条建议。

    **与 `llm/contracts.py` 的 `DailyReportNarrative` 同构**（`tests/test_reports_api.py`
    有一条门禁盯着）。同构是「两版能对比」的前提：人改的和模型写的必须是同一种
    形状，否则「这句话是谁写的」就没法逐段回答。

    但**不复用那个类**：LLM 层的契约跟着提示词演进，对外 API 的契约要稳定，两者
    该分开演进（设计文档第二节）。中间那道翻译在 `services/report.py`。

    🔴 **没有数字字段**，理由同 LLM 那边：日报里的数字全部来自快照列，这里只有
    措辞。人工修订时也一样 —— 要改数字得去改数据再重新生成，不能在文字里改。
    """

    #: 今天整体怎么样、变化可能来自什么。日报的价值就在这一段。
    summary: str = Field(min_length=1, max_length=800)

    #: 值得单独点出来的几件事，一条一句。
    highlights: list[str] = Field(default_factory=list, max_length=5)

    #: 🔴 **建议，永远只是建议。** 这个系统不碰广告平台的写接口。
    next_steps: list[str] = Field(default_factory=list, max_length=5)


class ReportActionItem(BaseModel):
    """日报里「本期做了什么」的一条。是**生成那一刻的副本**，不是关联查询。"""

    #: ISO 时刻字符串（快照里就是这么存的）。
    performed_at: str
    kind: str
    summary: str

    #: 为什么这么调。日报里最值钱的一段。
    reason: str
    object_name: str | None = None


class ReportGenerateRequest(BaseModel):
    """生成一份日报。"""

    #: 报告哪一天，**账户时区下的自然日**。口径见 docs/business/glossary.md。
    stat_date: date


class ReportReviseRequest(BaseModel):
    """存下人工修订后的那一版。

    **不能改数字，也不能改 LLM 原文** —— 前者要改数据再重新生成，后者永不修改。
    """

    narrative: ReportNarrative

    #: 谁改的。可空：一个人的团队填它没有意义。
    reviewer: str | None = Field(default=None, max_length=64)


class BaselineComparison(BaseModel):
    """对照期，以及由它算出来的 CPA。

    继承给两套日报出参，理由同 `DerivedMetrics`：**共用算法，不共用形状**。

    🔴 **三个字段同时为 `null` 是正常状态**：对照那天没有数据。此时前端应当把环比
    整段不显示，**不要拿 0 当基线** —— 那会算出一个凭空的「上升了 100%」。
    """

    #: 对照的是哪一天。默认上周同日，抵消星期几效应（glossary 的「周同比」）。
    baseline_date: date | None
    baseline_spend: Decimal | None
    baseline_conversions: Decimal | None

    @computed_field  # type: ignore[prop-decorator]  # pydantic 的装饰器与 property 的组合，mypy 认不出
    @property
    def baseline_cpa(self) -> Decimal | None:
        """对照期的 CPA。缺任何一项、或那天没有转化，都是 `None`（无定义）。"""
        if self.baseline_spend is None or self.baseline_conversions is None:
            return None
        return divide(self.baseline_spend, self.baseline_conversions)


class ReportItem(DerivedMetrics, BaselineComparison):
    """运营看到的一份日报：**两版人话都给**。

    继承 `DerivedMetrics` 而不是自己声明那五个数字：CPA / CTR / ROAS 的公式全项目
    只有一份，日报和看板上的同一个 CPA 必须是同一个数。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    stat_date: date
    status: ReportStatus

    #: 数据口径。日报必须注明是哪个时区的哪一天，否则客户拿自己后台的数字来对
    #: 永远差一截（glossary 的「时间口径」）。
    currency: str
    timezone: str

    # 五个基础数字 + 五个派生指标来自 DerivedMetrics，对照期来自 BaselineComparison。
    # 它们都是**生成那一刻固定下来的快照**，不随平台回填变化。金额和比率一律是
    # 字符串，前端不要用浮点解析（conventions.md）。

    actions_snapshot: list[ReportActionItem]

    #: 当时开着的告警摘要，规则算出来的事实（不是 LLM 写的）。
    alerts_snapshot: list[str]

    #: 🔴 模型原文，**永不修改**。`null` = 没生成（模型挂了或没配 LLM），
    #: 那时前端显示「未生成」，人自己写。
    llm_narrative: ReportNarrative | None

    #: 人工修订后的版本，客户看的是这个。`null` = 还没人改过，**发不出去**。
    narrative: ReportNarrative | None

    #: 哪次 LLM 调用生成的，对得上 `llm_calls` 里那一行（成本与提示词版本在那边）。
    llm_call_id: int | None

    #: 数字是什么时候固定下来的。
    generated_at: datetime

    #: 人工确认的时刻。**非空是发布的前置条件。**
    reviewed_at: datetime | None
    reviewer: str | None
    published_at: datetime | None


class ReportListResponse(BaseModel):
    items: list[ReportItem]
    total: int
