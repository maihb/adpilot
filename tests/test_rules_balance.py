"""余额可撑天数这条规则。

`conventions.md` 点名要求 `rules/` 的每条规则都有测试，理由是它决定要不要发告警，
而且是纯函数 —— 参数化测试成本极低，没有理由不覆盖完。

**这里一个数据库都不起。** 规则只收算好的数字，这正是把它单独分一层的回报。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adpilot.rules import balance as rules

D = Decimal


@pytest.mark.parametrize(
    ("available", "avg_daily_spend", "expected_days"),
    [
        (D(100), D(10), D("10.0")),
        (D(30), D(10), D("3.0")),
        # 除不尽的按 0.1 天取整。日报里没人需要「还能撑 2.3333333 天」。
        (D(70), D(30), D("2.3")),
        # 金额精度不丢：Decimal 一路走到底，中间不经 float。
        (D("12.3456"), D("1.2345"), D("10.0")),
    ],
)
def test_days_left_is_balance_over_daily_spend(
    available: Decimal,
    avg_daily_spend: Decimal,
    expected_days: Decimal,
) -> None:
    assert rules.runway(available, avg_daily_spend).days_left == expected_days


@pytest.mark.parametrize("avg_daily_spend", [D(0), D("-1")])
def test_no_recent_spend_is_undefined_and_never_alerts(avg_daily_spend: Decimal) -> None:
    """🔴 分母为 0 → 无定义，**不是 0，也不是无穷**。

    返回 0 会把它显示成「立刻要停」，返回无穷会显示成「永远够用」，两个都是谎。
    而且这种情况一定不能告警：没有消耗就不会归零，报一条「余额告急」只会让人对
    告警脱敏 —— 那比没有告警更糟。
    """
    result = rules.runway(D(1000), avg_daily_spend)

    assert result.days_left is None
    assert result.is_alerting is False


@pytest.mark.parametrize("available", [D(0), D("-50")])
def test_empty_balance_is_zero_days_and_alerts(available: Decimal) -> None:
    """余额见底 → 0 天且告警。广告此刻就是停的。

    不返回负数：负的「可撑天数」没有意义，0 已经把「该充钱了」说得足够清楚。
    """
    result = rules.runway(available, D(10))

    assert result.days_left == 0
    assert result.is_alerting is True


@pytest.mark.parametrize(
    ("days_left", "should_alert"),
    [
        (D("2.9"), True),
        # 🔴 恰好等于阈值就要告警：阈值的语义是「留给人反应的时间」，卡在边界上
        # 时那点时间已经不够了。这条用 < 写就会漏掉。
        (D("3.0"), True),
        (D("3.1"), False),
    ],
)
def test_threshold_is_inclusive(days_left: Decimal, should_alert: bool) -> None:
    # 日均固定 10，余额直接由目标天数反推，免得在断言里再算一次
    result = rules.runway(days_left * 10, D(10))

    assert result.days_left == days_left
    assert result.is_alerting is should_alert


def test_threshold_travels_with_the_result() -> None:
    """阈值跟着结果一起回出去，前端不必再抄一份 —— 抄的那份改不动。"""
    assert rules.runway(D(100), D(10)).threshold_days == rules.ALERT_THRESHOLD_DAYS


class TestAverageDailySpend:
    """日均消耗的分母。**这是整条规则里最容易错的一处。**"""

    def test_divides_by_days_that_actually_have_data(self) -> None:
        """🔴 分母是「真有数据的天数」，不是回看窗口的 7。

        某天没导入 ≠ 那天没花钱。用 7 当分母会让日均被系统性拉低 → 可撑天数被
        高估 → **该告警的账户不告警**，而这是最危险的错误方向。
        """
        # 一周里只导进来 3 天，共花了 300
        assert rules.average_daily_spend(D(300), days_with_data=3) == D(100)

        # 用 LOOKBACK_DAYS 当分母的话会算成 ~42.9，可撑天数凭空翻一倍多
        assert rules.average_daily_spend(D(300), rules.LOOKBACK_DAYS) != D(100)

    def test_paused_days_still_count(self) -> None:
        """真正暂停投放的日子该被算进去：那些天有指标行、spend 是 0。

        区分「没数据」和「没花钱」正是这个分母的全部意义。
        """
        assert rules.average_daily_spend(D(300), days_with_data=6) == D(50)

    @pytest.mark.parametrize("days", [0, -1])
    def test_no_data_at_all_yields_zero_which_reads_as_undefined(self, days: int) -> None:
        """一天数据都没有 → 0，再由 runway() 判成「无定义」。

        两步分开是有意的：这个函数不认识「无定义」这个概念，它只做除法。
        """
        assert rules.average_daily_spend(D(0), days) == 0
        assert rules.runway(D(1000), rules.average_daily_spend(D(0), days)).days_left is None
