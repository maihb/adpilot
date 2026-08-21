"""告警的出参。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AlertItem(BaseModel):
    """一条告警。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int

    #: `balance_low` / `metric_anomaly`
    kind: str

    #: `open` / `resolved`
    status: str

    #: 这条告警说的是账户里的哪件事：`balance`、`metric:spend`、`metric:cpa`。
    #: 同一个 (账户, 种类, subject) 同时只会有一条 `open`。
    subject: str

    #: 人话摘要，直接能贴进日报。**由规则算出来的事实，不是 LLM 写的解释。**
    message: str

    #: 触发时算出来的数字，形状随 `kind` 变。金额和比率都是**字符串**（conventions
    #: 那条「JSON 里金额是字符串」），前端不要用浮点解析。
    #:
    #: 这是**快照**：日报要能写出「触发时还剩 2.3 天」，而那时余额早就变了。
    detail: dict[str, Any]

    #: 问题是什么时候被发现的。**不随后续巡检更新** —— 「持续了多久」靠它算。
    opened_at: datetime

    #: 最近一次巡检确认它仍然成立。和 `opened_at` 差得越远，说明拖得越久。
    last_seen_at: datetime

    resolved_at: datetime | None

    #: 推送成功的时刻。`null` 表示还没推出去（没配 webhook，或者推失败了）。
    #: **只在告警新开时推一次**，不会每轮巡检重发。
    notified_at: datetime | None


class AlertListResponse(BaseModel):
    items: list[AlertItem]
    total: int


class SweepResponse(BaseModel):
    """一轮巡检的结果。数字都是**告警条数**，不是账户数。"""

    model_config = ConfigDict(from_attributes=True)

    accounts: int

    #: 这一轮新发现的
    opened: int

    #: 之前就开着、这一轮确认仍然成立的
    still_open: int

    #: 这一轮确认问题不在了、被置成 resolved 的
    resolved: int

    #: 推送成功的条数。没配 webhook 时恒为 0，那是正常状态不是故障。
    notified: int


class DiagnosisItem(BaseModel):
    """一条告警的解释。**只有文字，没有会被执行的东西。**

    与 `llm/contracts.py` 的 `Diagnosis` 同构，但不复用那个类：LLM 层的契约跟着
    提示词演进，对外 API 的契约要稳定（设计文档第二节）。
    """

    #: 最可能的原因，按可能性从高到低。
    likely_causes: list[str]

    #: 人接下来该去核实什么。
    suggested_checks: list[str]

    #: 🔴 一句话建议。**没有「把预算改成 X」这种字段** —— 这个系统不碰广告平台的
    #: 写接口，它给方向，定多少是人拿着完整上下文决定的事。
    suggestion: str


class DiagnosisResponse(BaseModel):
    """诊断的结果。

    ⚠️ **`diagnosis` 为 `null` 是正常返回，不是错误。** 模型这次没答上来（挂了，
    或者连着几次输出都不合格）时走这条路 —— 接口仍然 200，因为告警本身和它带的
    那些数字全都还在，诊断只是锦上添花。

    为什么不回 503：那次调用**已经烧了 token 并落了账**，而抛异常会让整个事务
    回滚、把那条账一起抹掉（`services/llm.py` 的模块 docstring 讲了这条）。
    """

    diagnosis: DiagnosisItem | None

    #: 这次调用在 `llm_calls` 里的那一行。`diagnosis` 为空时靠它查为什么。
    llm_call_id: int
