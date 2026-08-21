"""投放操作记录的出入参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from adpilot.models.action import ActionKind
from adpilot.models.daily_metric import MetricLevel

#: `reason` 的长度上限。库里那一列是 `Text`（不限长），这里卡一刀是为了拦住「把
#: 一整份复盘文档贴进来」—— 日报要引用的是一两句话，而超长的那一段进了提示词就
#: 是白烧的 token。真需要长篇的，写在自己的文档里，这里留一句结论。
REASON_MAX_LENGTH = 1000


class ActionCreateRequest(BaseModel):
    """登记一次投放调整。

    **没有 `source`**：手动登记的一律是 `manual`。放一个参数出去，人手工登记时
    随手标成「平台抓的」，`reason` 到底可不可信就再也答不上来了。
    """

    kind: ActionKind

    #: 动在哪一级。默认账户级 —— 「整体调了日预算」是最常见的一类，让它不必填。
    level: MetricLevel = MetricLevel.ACCOUNT

    #: 平台侧的对象 ID。账户级操作留空：把账户自己的 external_id 填进来不增加
    #: 任何信息。**认对象一律认它**，名字只是给人看的。
    object_id: str | None = Field(default=None, max_length=64)
    object_name: str | None = Field(default=None, max_length=256)

    #: 做了什么，一行人话，直接能进日报（「A 系列日预算 500 → 800」）。
    summary: str = Field(min_length=1, max_length=256)

    #: 🔴 **为什么这么做。** 这一段是这张表和平台变更日志的唯一区别 —— 平台记得住
    #: 「预算从 500 改成了 800」，记不住「周末 CPM 普涨，先扛量到周一再看」。
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)

    #: 操作**实际发生**的时刻，不是登记时刻。必须带时区（同 `captured_at`），
    #: 且不能在未来 —— 落在未来的记录永远不会进任何一期日报。
    performed_at: datetime

    #: 谁做的。可空：一个人的团队填它没有意义。
    operator: str | None = Field(default=None, max_length=64)

    @field_validator("performed_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """拒绝不带时区的时刻。

        放行的话它会被按服务器本地时区存进 `timestamptz`，而服务器时区和账户时区
        常常不是一回事 —— 于是一条傍晚做的调整在日报里落到了前一天。偏几小时不会
        让任何东西报错，只会让「本期做了什么」和当期指标对不上。
        """
        if value.tzinfo is None:
            raise ValueError("performed_at 必须带时区，例如 2026-08-20T15:30:00+08:00")
        return value


class ActionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int

    kind: ActionKind

    #: `manual`（人填的）/ `platform`（从平台变更日志抓的，🚧 还没有写入路径）。
    #: 两者的区别是 `reason` 可不可信 —— 平台不记录人为什么这么调。
    source: str

    level: MetricLevel
    object_id: str | None
    object_name: str | None

    summary: str
    reason: str

    #: 操作实际发生的时刻。`created_at` 是登记时刻，两者差几小时是常态。
    performed_at: datetime
    operator: str | None
    created_at: datetime


class ActionListResponse(BaseModel):
    items: list[ActionItem]
    total: int
