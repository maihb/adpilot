"""数据停更判定。

`conventions.md` 点名要求 `rules/` 的每条规则都有测试 —— 它决定要不要发告警，
而且是纯函数。这一条尤其值得覆盖完：它是**元规则**，它成立的时候另外三条规则
（余额、异动、库存）的结论全都不可信。

一个数据库都不起。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adpilot.rules import fetch as rules

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
STALE_HOURS = 26


def _evaluate(
    *,
    hours_ago: float | None,
    failures: int = 0,
) -> rules.FetchHealth:
    last_success = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return rules.evaluate(
        last_success_at=last_success,
        consecutive_failures=failures,
        now=NOW,
        stale_hours=STALE_HOURS,
    )


def test_a_recent_success_is_healthy() -> None:
    assert _evaluate(hours_ago=2).is_alerting is False


def test_one_failure_does_not_alert() -> None:
    """一次失败不报 —— 任务自己会退避重试，多半在同一轮里就自愈了。

    **这不是容忍故障，是让告警值得被相信**：一条每周误报几次的告警，三周之后
    就没人看了。
    """
    assert _evaluate(hours_ago=2, failures=1).is_alerting is False


def test_two_failures_in_a_row_alert_as_failing() -> None:
    health = _evaluate(hours_ago=2, failures=2)

    assert health.trouble is rules.FetchTrouble.FAILING
    assert health.consecutive_failures == 2


def test_no_error_but_too_long_since_success_alerts_as_stale() -> None:
    """🔴 失败计数为 0 也可能停更，而那种情况最危险。

    典型成因是排期任务根本没在跑（beat 没起、worker 死了、账户被从扫描范围里
    漏掉）—— 那些故障**不经过任何一条 except**，所以没有任何错误信息，只有一个
    越来越旧的时间戳。
    """
    health = _evaluate(hours_ago=30)

    assert health.trouble is rules.FetchTrouble.STALE
    assert health.hours_since_success == 30


@pytest.mark.parametrize("hours_ago", [24, 25, 25.9])
def test_a_day_and_a_bit_is_still_healthy(hours_ago: float) -> None:
    """阈值定在 26 小时而不是 24。

    日切、平台自己的延迟、夏令时那天不是 24 小时，都会让「上次成功」的间隔
    天然地略超一天。卡在 24 会让告警每周误报几次。
    """
    assert _evaluate(hours_ago=hours_ago).is_alerting is False


def test_exactly_at_the_threshold_alerts() -> None:
    """边界含等号：26 小时整就报。阈值的含不含等号是这类规则最容易写反的地方。"""
    assert _evaluate(hours_ago=STALE_HOURS).trouble is rules.FetchTrouble.STALE


def test_failing_wins_over_stale() -> None:
    """两个条件同时成立时报 `FAILING`。

    因为它带着 `last_error`，能告诉人下一步做什么；而 `STALE` 只能说「不知道
    为什么，反正没数」。
    """
    assert _evaluate(hours_ago=100, failures=3).trouble is rules.FetchTrouble.FAILING


def test_never_succeeded_but_not_yet_failing_stays_quiet() -> None:
    """从没成功过、失败次数也没到阈值 → 先不报。

    这是「刚挂上凭据、还没跑到第一个排期点」的形状。真的一直不成功，失败计数
    很快会攒够，那时按 `FAILING` 报（而且带着原因）。
    """
    health = _evaluate(hours_ago=None, failures=1)

    assert health.is_alerting is False
    assert health.hours_since_success is None


def test_a_clock_skew_never_produces_negative_hours() -> None:
    """时钟回拨、或时间戳来自另一台机器时，差值可以是负的。

    让它变成 0 而不是负数：一个「-3 小时没成功」的告警文案会让人怀疑整套系统，
    而它的实际含义就是「刚刚成功过」。
    """
    health = rules.evaluate(
        last_success_at=NOW + timedelta(hours=3),
        consecutive_failures=0,
        now=NOW,
        stale_hours=STALE_HOURS,
    )

    assert health.hours_since_success == 0
    assert health.is_alerting is False
