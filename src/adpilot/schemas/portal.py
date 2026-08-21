"""客户端（`/api/portal/*`）的出参。

🔴 **和内部那套不共用 response model**，哪怕字段一模一样。

理由不是洁癖：同一个模型服务两类人的时候，**加字段的方向永远是往外漏**。今天
给告警加一个 `notified_at`（推送成功没有，纯运维信息），明天给客户加一个结算
备注 —— 每一次都要有人想起「这个字段客户能不能看」，而想不起来是不会报错的。
两套模型的代价是这个文件里几十行声明，换来的是那个问题**只在这里出现一次**。

下面 `PortalAlertItem` 已经是个现成的例子：它比 `AlertItem` 少一个字段。

派生指标的计算从 `schemas/daily_metric.py` 复用 —— 重复的是**形状声明**，不是
**计算逻辑**（设计文档第二节末尾那句「重复的是调用，不是逻辑」）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from adpilot.models.ad_account import Platform
from adpilot.schemas.daily_metric import DerivedMetrics
from adpilot.schemas.report import BaselineComparison, ReportActionItem, ReportNarrative


class PortalProfileResponse(BaseModel):
    """客户看自己。

    **没有 `note`** —— 那是内部备注（「这家结算总拖」之类），也没有 `is_active`：
    停止合作的客户根本进不来（`api/deps.py` 的 `require_client_scope`），所以这个
    字段在这里恒为 true，等于噪音。
    """

    id: int
    name: str


class PortalAccountItem(BaseModel):
    """客户的一个广告账户。

    **没有 `external_id`**：平台侧的账户 ID 是回平台核对时的锚点，属于运营的
    工作面，客户端不需要它来做任何事。少一个是一个。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    platform: Platform

    #: 这两个字段决定了下面所有数字怎么读：金额是这个币种，`stat_date` 是这个
    #: 时区下的自然日。缺一个都会让客户拿自己后台的数字来对时对不上。
    currency: str
    timezone: str

    is_active: bool


class PortalAccountListResponse(BaseModel):
    items: list[PortalAccountItem]
    total: int


class PortalMetricDay(DerivedMetrics):
    """时间线上的一天。派生指标（CPM/CPC/CTR/CPA/ROAS）由基类现算。"""

    model_config = ConfigDict(from_attributes=True)

    stat_date: date


class PortalMetricsResponse(BaseModel):
    """一段时间线，外加读懂它必需的口径。

    `currency` 和 `timezone` 放在这一层而不是每天重复一遍：它们是整个账户的属性。
    """

    account_id: int
    currency: str
    timezone: str

    #: 没有数据的那天**不在里面**，不补零 —— 「那天花了 0」和「那天没导入」是
    #: 两件事，前端画折线时要能分开（断线，不是落到 0）。
    items: list[PortalMetricDay]


class PortalRunwayResponse(BaseModel):
    """余额还能撑多久。

    **`available` 为 null 表示从没录过余额**，此时其余字段也都是 null —— 那是
    「不知道」，不是「没事」。造一个「余额 0」出来，会让每个刚建好的账户看起来
    都在着火。
    """

    account_id: int
    currency: str

    available: Decimal | None
    avg_daily_spend: Decimal | None

    #: 🔴 `null` 表示无定义（近期没花钱），既不是 0 也不是「永远够用」。
    days_left: Decimal | None

    is_alerting: bool
    threshold_days: Decimal | None

    #: 用的是哪条余额快照（它说自己是什么时刻的）。
    captured_at: datetime | None

    #: 日均消耗的回看窗口，闭区间，账户时区下的自然日。**不含今天**。
    lookback_from: date | None
    lookback_to: date | None

    #: 窗口里真有数据的天数。明显小于窗口长度就说明有几天没导入，那时这个日均
    #: （以及可撑天数）要打个问号。
    days_with_data: int | None


class PortalAlertItem(BaseModel):
    """一条告警。

    **比内部的 `AlertItem` 少一个 `notified_at`** —— 推送成功没有是运维信息，
    跟客户无关。这就是这个文件存在的理由。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int

    #: 指向哪个投放账户。**`null` 表示这是客户级告警**（`stock_low`：商品挂在
    #: 客户上，同一批货可能被多个账户在推）。客户端那一屏据此决定要不要显示账户名。
    #:
    #: **不带 `client_id`** —— 客户端只看得到自己的告警，那个字段对它恒等于
    #: 「我自己」，回出去只是噪音（同这个文件里去掉 `notified_at` 的理由）。
    account_id: int | None

    kind: str
    status: str
    subject: str

    #: 人话摘要，直接能显示。**规则算出来的事实，不是 LLM 写的解释。**
    message: str

    #: 触发时算出来的数字。金额和比率都是**字符串**，前端不要用浮点解析。
    detail: dict[str, Any]

    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class PortalAlertListResponse(BaseModel):
    items: list[PortalAlertItem]
    total: int


class PortalReportItem(DerivedMetrics, BaselineComparison):
    """客户看到的一份日报。

    🔴 **没有 `llm_narrative`，也没有 `status`。** 客户看的是人确认过的那一版，
    模型原文是内部的审计信息（用来回答「这句话是模型写的还是人改的」），不该出现在
    客户端 —— 那等于把「这段话是 AI 写的、我们只是过了一眼」直接摆出去。有一条门禁
    盯着这件事（`tests/test_reports_api.py`）。

    数字是**生成那一刻的快照**，不随平台回填变化。派生指标（CPA / CTR / ROAS）由
    后端按 [glossary](../../../docs/business/glossary.md) 的公式现算，和看板共用
    同一份实现；金额和比率一律是**字符串**，前端不要用浮点解析
    （`client/src/utils/` 是唯一允许转换的地方）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int

    #: 报告的是哪一天，**账户时区**下的自然日 —— `timezone` 一起下发就是为了让
    #: 前端把这句口径显示出来。不注明的话客户拿自己后台的数字来对永远差一截。
    stat_date: date
    currency: str
    timezone: str

    # 五个基础数字 + 派生指标（CPA / CTR / ROAS）来自 DerivedMetrics，对照期与
    # baseline_cpa 来自 BaselineComparison —— 和看板用的是同一份公式。

    #: 人确认过的那段话。已发布的日报一定有它。
    narrative: ReportNarrative

    #: 本期做了什么，含「为什么这么调」。这是日报的交付价值所在。
    actions: list[ReportActionItem]

    #: 当时开着的告警摘要，规则算出来的事实。
    alerts: list[str]

    published_at: datetime


class PortalReportListResponse(BaseModel):
    items: list[PortalReportItem]
    total: int
