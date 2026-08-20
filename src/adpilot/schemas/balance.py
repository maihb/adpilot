"""余额快照与余额告警的出入参。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BalanceCreateRequest(BaseModel):
    """录一条余额快照。

    **没有 `currency`**：币种取账户的。让调用方传一个进来，早晚会出现「账户是
    USD、余额录成 CNY」的一条快照 —— 而算出来的可撑天数看起来完全正常。
    """

    #: 可用余额。允许 0（真的花光了），不允许负数 —— 那一定是填错了。
    available: Decimal = Field(ge=0)

    #: 这个余额是**什么时候**的，不是录入时间：人可能今天补录昨天看到的数。
    #: 必须带时区 —— 裸 naive datetime 在本项目里一律视为 bug（conventions.md）。
    captured_at: datetime

    #: 从哪看来的、是不是刚充完值。日报要说清余额的口径，而这行字是唯一能说清
    #: 「为什么当时是这个数」的地方。
    note: str | None = Field(default=None, max_length=256)

    @field_validator("captured_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """拒绝不带时区的时刻。

        放行的话，它会被当成服务器本地时区存进 `timestamptz`，而服务器时区和账户
        时区常常不是一回事 —— 于是「昨天下午看到的余额」落库时悄悄偏了几个小时。
        偏几小时不会让任何东西报错，只会让日报里的口径说不清。
        """
        if value.tzinfo is None:
            raise ValueError("captured_at 必须带时区，例如 2026-08-20T10:00:00+08:00")
        return value


class BalanceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    available: Decimal

    #: 账户币种。余额和消耗必须是同一种货币才能相除。
    currency: str

    captured_at: datetime
    note: str | None


class BalanceListResponse(BaseModel):
    items: list[BalanceItem]
    total: int


class BalanceRunwayResponse(BaseModel):
    """一个账户的余额还能撑多久。"""

    account_id: int
    account_name: str
    currency: str

    available: Decimal

    #: 近 N 天的日均消耗。分母是「真有数据的天数」而不是 N —— 某天没导入不等于
    #: 那天没花钱，用 N 当分母会系统性高估可撑天数。
    avg_daily_spend: Decimal

    #: 🔴 **`null` 表示无定义**（近期没花钱），既不是 0 也不是「永远够用」。
    #: 此时 `is_alerting` 一定是 false：没有消耗就不会归零。
    days_left: Decimal | None

    #: 低于阈值即为真。阈值本身一并回出去，前端不必再抄一份。
    is_alerting: bool
    threshold_days: Decimal

    #: 用的是哪条余额快照（它说自己是什么时刻的）。日报里要注明这个口径。
    captured_at: datetime

    #: 日均消耗的回看窗口，闭区间，账户时区下的自然日。**不含今天** —— 今天的
    #: 数据还没跑完，算进去会把日均拉低。
    lookback_from: date
    lookback_to: date

    #: 窗口里真正有数据的天数。明显小于窗口长度就说明有几天没导入，那时这个
    #: 日均（以及可撑天数）要打个问号。
    days_with_data: int


class BalanceAlertListResponse(BaseModel):
    items: list[BalanceRunwayResponse]
    total: int
