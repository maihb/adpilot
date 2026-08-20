"""指标异动：昨天和上周同一天比，变化超过阈值就标记。

**规则只负责「涨了 40%」这个判定，「大概率是素材疲劳，建议换素材」是 LLM 那一层的
事** —— 两者不能混在一起写（[设计文档第五节](../../../docs/design/2026-08-19-mvp-design.md)）。
所以这个模块的产出是一个数字和一个布尔值，没有一句自然语言。

## 为什么是周同比，不是日环比

[glossary](../../../docs/business/glossary.md) 里两个都列了，MVP 只实现周同比，
因为**星期几效应是这个领域最强的周期性噪音源**：广告花费和 CPM 在周末普遍上涨，
日环比会在每个周六和周一各制造一批假告警。设计文档举的那个日报例子原文就是
「成本上升是周末 CPM 普涨，未做调整」—— 一条规则如果每周固定误报两次，人会先学会
忽略它，然后错过真的那一次。

昨天 vs 上周同一天，星期几对齐，这个噪音自动消掉。代价是需要 8 天数据，新账户前
一周判不了 —— 那时返回「没有基线」，而不是拿一个凑合的基线硬判。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

#: 上周同一天。名字写成常量而不是散在代码里的 7，是因为它和「周同比」这个口径
#: 绑死：改成别的数字，这条规则就不再是周同比了，得先去改 glossary。
COMPARISON_LAG_DAYS = 7

#: ⚠️ **待定参数，当前取 40%。** 设计文档举的例子就是「CPA 涨了 40%」。
#: 定下来的那天只改这一行。
CHANGE_THRESHOLD = Decimal("0.4")

#: ⚠️ **待定参数，当前取 10（账户币种）。** 小额消耗的比率毫无意义 —— 花 1 块变成
#: 花 2 块就是 +100%，而那不是任何人需要知道的事。两天的花费都低于这个数就不判。
#: 用绝对值而不是比例是因为没有别的锚点可用；换算成人民币的问题留给真有多币种
#: 客户的那天。
MIN_SPEND = Decimal(10)

_SCALE = Decimal("0.0001")


class AnomalyMetric(StrEnum):
    """哪些指标值得盯。

    只有两个，是刻意的：**告警清单的价值和它的长度成反比。** 花费管住「投出去的
    钱有没有失控」，CPA 管住「花出去的钱有没有变贵」，其余指标（CTR、CPM）多半是
    这两个的解释而不是独立的问题。
    """

    SPEND = "spend"
    CPA = "cpa"


class AnomalyDirection(StrEnum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class Anomaly:
    """一个指标的周同比结果。`is_anomalous` 为真才值得告警。"""

    metric: AnomalyMetric
    current: Decimal
    baseline: Decimal
    change_ratio: Decimal
    direction: AnomalyDirection
    is_anomalous: bool
    threshold: Decimal = CHANGE_THRESHOLD


def compare(
    metric: AnomalyMetric,
    current: Decimal | None,
    baseline: Decimal | None,
    *,
    current_spend: Decimal,
    baseline_spend: Decimal,
) -> Anomaly | None:
    """比一个指标，返回判定；比不了就返回 `None`。

    「比不了」有三种，都返回 `None` 而不是硬判 —— 一个基于烂基线的告警比没有告警更
    有害，因为它看起来和真的一模一样：

    * **缺任何一边的值。** CPA 在没有转化的那天是 `null`（无定义，不是 0），
      拿它当基线会算出一个凭空的百分比。
    * **基线为 0。** 除数是 0，比率不存在。「从 0 涨到 100」在数学上不是「涨了
      百分之几」，硬要表达只能是无穷。
    * **两天花费都太小。** 比率被噪音主导，见 `MIN_SPEND`。

    **CPA 只报涨、不报跌。** 成本下降是好消息，塞进告警清单只会稀释真正要处理的
    那几条。花费则涨跌都报：暴涨可能是出价失控，暴跌可能是被拒审或预算见底 ——
    两个方向都要人当天知道。
    """
    if current is None or baseline is None or baseline <= 0:
        return None
    if current_spend < MIN_SPEND and baseline_spend < MIN_SPEND:
        return None

    change_ratio = ((current - baseline) / baseline).quantize(_SCALE)
    direction = AnomalyDirection.UP if change_ratio >= 0 else AnomalyDirection.DOWN

    is_anomalous = abs(change_ratio) >= CHANGE_THRESHOLD
    if metric is AnomalyMetric.CPA and direction is AnomalyDirection.DOWN:
        is_anomalous = False

    return Anomaly(
        metric=metric,
        current=current,
        baseline=baseline,
        change_ratio=change_ratio,
        direction=direction,
        is_anomalous=is_anomalous,
    )


def cost_per_action(spend: Decimal, conversions: Decimal) -> Decimal | None:
    """CPA = 花费 / 转化数，**分母为 0 返回 `None`**（无定义）。

    公式在 [glossary](../../../docs/business/glossary.md) 里，出参那边
    （`schemas/daily_metric.py`）也算了一份 —— 两处算的是同一个口径，但那边算的是
    「给人看的一行」，这边算的是「拿来判定的一个数」，且这边够不着 `schemas`
    （分层契约）。哪天口径变了，glossary 是唯一的真相源，两处都得跟着改。
    """
    if conversions <= 0:
        return None
    return (spend / conversions).quantize(_SCALE)
