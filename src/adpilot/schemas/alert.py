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
