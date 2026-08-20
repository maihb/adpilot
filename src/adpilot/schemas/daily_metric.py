"""日指标的出参。

派生指标（CPM / CPC / CTR / CPA / ROAS）**在这里现算，不存库**。存下来就等于把
公式复制进了数据库：口径一改、或者平台回填了历史数据，存的那份和算的那份立刻
对不上，而「同一个 CPA 出现两个值」会让整套输出的可信度归零。

公式的真相源是 [glossary](../../../docs/business/glossary.md) 的「指标」一节。
计算函数放在这一层而不是 `services/`，是因为分层契约里 `schemas` 在 `services`
**之下** —— 而且这些数字只在对外表达时才存在，它们本来就是出参形状的一部分。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field

from adpilot.models.daily_metric import MetricLevel

# 派生指标统一保留的小数位。Decimal 除法默认给 28 位有效数字，直接序列化出去是
# 一串没人看的尾数；6 位对金额（4 位）和比率都够，且不会出现无限循环小数。
_SCALE = Decimal("0.000001")


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """🔴 分母为 0 一律返回 `None`（无定义），**不返回 0，也不返回无穷**。

    写 0 会让「今天没有转化」和「今天 CPA 是 0 元」变成同一个显示值 —— 而这两件
    事在日报里天差地别（glossary「指标」一节开头那条）。
    """
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(_SCALE)


class DerivedMetrics(BaseModel):
    """五个基础数字，外加由它们现算出来的派生指标。

    **单独一个基类，是为了让两套出参共用「算法」而不共用「形状」**：内部的
    `DailyMetricItem` 和客户端的 `PortalMetricDay` 各自声明自己的字段，公式却只有
    这一份。公式抄第二遍的代价是「CPA 在两个页面上不一样」——那种 bug 没人会想到
    去查两个 schema。
    """

    spend: Decimal
    impressions: int
    clicks: int
    conversions: Decimal
    revenue: Decimal

    @computed_field  # type: ignore[prop-decorator]  # pydantic 的装饰器与 property 的组合，mypy 认不出
    @property
    def cpm(self) -> Decimal | None:
        return _divide(self.spend * 1000, Decimal(self.impressions))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpc(self) -> Decimal | None:
        return _divide(self.spend, Decimal(self.clicks))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ctr(self) -> Decimal | None:
        """点击率存小数不存百分数，展示时再乘 100。"""
        return _divide(Decimal(self.clicks), Decimal(self.impressions))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpa(self) -> Decimal | None:
        return _divide(self.spend, self.conversions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def roas(self) -> Decimal | None:
        return _divide(self.revenue, self.spend)


class DailyMetricItem(DerivedMetrics):
    """内部接口的一行：带层级和对象，客户端那套没有这几样。"""

    model_config = ConfigDict(from_attributes=True)

    stat_date: date
    level: MetricLevel
    object_id: str
    object_name: str | None

    #: 账户币种，**不是人民币**。跨账户汇总前必须先说清楚要不要换算。
    currency: str

    #: 🔴 跨天不可加：同一个人两天都被触达，相加会把他算两次。周期汇总的 reach
    #: 必须向平台单独请求那个周期的值。可空是因为平台不一定给。
    reach: int | None


class DailyMetricListResponse(BaseModel):
    items: list[DailyMetricItem]
    total: int


class NormalizeResponse(BaseModel):
    """一次归一化的结果。"""

    model_config = ConfigDict(from_attributes=True)

    account_id: int

    #: 这次覆盖到的自然日，升序
    days: list[date]

    #: 写进 `daily_metrics` 的行数（upsert，所以「更新」也算在内）
    rows: int

    #: 用到了几条快照。同一个 (层级, 日期) 只取 `fetched_at` 最新的那条，所以
    #: 这个数**不等于**该账户的快照总数。
    snapshots: int

    #: 因为缺对象 ID 被跳过的行数，通常是导出文件里残留的小计行
    skipped_rows: int
