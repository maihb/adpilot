"""指标异动这条规则。

和余额那条一样：纯函数、一个数据库都不起、边界靠参数化表格覆盖完。

这条规则的难点不在算百分比，在**什么时候不该出声** —— 一个基于烂基线的告警比没有
告警更有害，因为它看起来和真的一模一样。所以下面「返回 None」的用例比「判定为异动」
的用例还多。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adpilot.rules import anomaly as rules
from adpilot.rules.anomaly import AnomalyDirection, AnomalyMetric

D = Decimal
SPEND = AnomalyMetric.SPEND
CPA = AnomalyMetric.CPA

# 两边都远高于 MIN_SPEND，免得噪音门槛干扰到「算比率」那一组用例
_BIG = D(1000)


def _compare(
    metric: AnomalyMetric,
    current: Decimal | None,
    baseline: Decimal | None,
    *,
    current_spend: Decimal = _BIG,
    baseline_spend: Decimal = _BIG,
) -> rules.Anomaly | None:
    return rules.compare(
        metric,
        current,
        baseline,
        current_spend=current_spend,
        baseline_spend=baseline_spend,
    )


@pytest.mark.parametrize(
    ("current", "baseline", "expected_ratio", "expected_direction"),
    [
        (D(140), D(100), D("0.4"), AnomalyDirection.UP),
        (D(60), D(100), D("-0.4"), AnomalyDirection.DOWN),
        (D(250), D(100), D("1.5"), AnomalyDirection.UP),
        (D(100), D(100), D(0), AnomalyDirection.UP),
    ],
)
def test_change_ratio_is_relative_to_the_baseline(
    current: Decimal,
    baseline: Decimal,
    expected_ratio: Decimal,
    expected_direction: AnomalyDirection,
) -> None:
    result = _compare(SPEND, current, baseline)

    assert result is not None
    assert result.change_ratio == expected_ratio
    assert result.direction is expected_direction


@pytest.mark.parametrize(
    ("current", "should_fire"),
    [
        (D(139), False),
        # 恰好等于阈值就要报：和余额那条阈值一样用 >=，理由也一样 —— 卡在边界上
        # 时「还没到」这个说法已经没有意义了。
        (D(140), True),
        (D(141), True),
    ],
)
def test_threshold_is_inclusive(current: Decimal, should_fire: bool) -> None:
    result = _compare(SPEND, current, D(100))

    assert result is not None
    assert result.is_anomalous is should_fire


def test_spend_fires_in_both_directions() -> None:
    """花费涨跌都要报。

    暴涨可能是出价失控，暴跌可能是被拒审或预算见底 —— 两个方向都要人当天知道。
    """
    up = _compare(SPEND, D(200), D(100))
    down = _compare(SPEND, D(20), D(100))

    assert up is not None and up.is_anomalous is True
    assert down is not None and down.is_anomalous is True


def test_cpa_only_fires_when_it_gets_worse() -> None:
    """🔴 CPA 只报涨、不报跌。

    成本下降是好消息，塞进告警清单只会稀释真正要处理的那几条。**比率照样算出来**
    （日报可能想说「成本降了 40%」），只是 `is_anomalous` 为假。
    """
    worse = _compare(CPA, D(200), D(100))
    better = _compare(CPA, D(50), D(100))

    assert worse is not None and worse.is_anomalous is True

    assert better is not None
    assert better.is_anomalous is False
    assert better.change_ratio == D("-0.5")  # 数字还在，只是不告警


class TestWhenItShouldStaySilent:
    """三种「比不了」，一律返回 None 而不是硬判。"""

    @pytest.mark.parametrize(
        ("current", "baseline"),
        [(None, D(100)), (D(100), None), (None, None)],
    )
    def test_missing_either_side(self, current: Decimal | None, baseline: Decimal | None) -> None:
        """CPA 在没有转化的那天是 null（无定义，不是 0）。

        拿它当基线会算出一个凭空的百分比 —— 而那条告警和真的长得一模一样。
        """
        assert _compare(CPA, current, baseline) is None

    @pytest.mark.parametrize("baseline", [D(0), D("-1")])
    def test_zero_baseline(self, baseline: Decimal) -> None:
        """除数是 0，比率不存在。

        「从 0 涨到 100」在数学上不是「涨了百分之几」，硬要表达只能是无穷。
        """
        assert _compare(SPEND, D(100), baseline) is None

    def test_both_days_below_the_noise_floor(self) -> None:
        """小额消耗的比率毫无意义：花 1 块变成花 2 块就是 +100%。"""
        tiny = rules.MIN_SPEND - D(1)

        assert _compare(SPEND, tiny, D(1), current_spend=tiny, baseline_spend=D(1)) is None

    def test_one_side_above_the_floor_still_counts(self) -> None:
        """只要有一天花得够多就照常判。

        「昨天花了 500、上周同日花了 2 块」正是最该被看见的那种跳变，用 and 而不是
        or 写门槛就会把它漏掉。
        """
        result = _compare(SPEND, D(500), D(2), current_spend=D(500), baseline_spend=D(2))

        assert result is not None
        assert result.is_anomalous is True


class TestCostPerAction:
    def test_divides_spend_by_conversions(self) -> None:
        assert rules.cost_per_action(D(100), D(4)) == D(25)

    @pytest.mark.parametrize("conversions", [D(0), D("-1")])
    def test_no_conversions_is_undefined_not_zero(self, conversions: Decimal) -> None:
        """分母为 0 返回 None —— 与 glossary 里那条通用规矩一致。

        写 0 会让「今天没有转化」和「今天 CPA 是 0 元」变成同一个值，而这两件事在
        日报里天差地别。
        """
        assert rules.cost_per_action(D(100), conversions) is None

    def test_fractional_conversions_are_supported(self) -> None:
        """平台按归因比例分配时会给出小数转化数，`daily_metrics` 也是这么存的。"""
        assert rules.cost_per_action(D(100), D("2.5")) == D(40)


def test_comparison_is_week_over_week_not_day_over_day() -> None:
    """口径本身也钉一条。

    改成别的数字这条规则就不再是周同比了 —— 星期几效应会重新混进来，而它是这个
    领域最强的周期性噪音源（设计文档里「周末 CPM 普涨」那个例子）。改之前先去改
    glossary。
    """
    assert rules.COMPARISON_LAG_DAYS == 7
