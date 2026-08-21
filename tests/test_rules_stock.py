"""库存可撑天数这条规则。

两半：`runway()` 那个除法（和余额那条同构，边界一一对应），以及
`infer_daily_sales()` —— **没有现成销量列时从库存快照序列推日均**，那是这条规则
独有的、也是唯一有猜测成分的一段，所以边界要钉得比除法还紧。

**这里一个数据库都不起。** 规则只收算好的数字，这正是把它单独分一层的回报。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adpilot.rules import stock as rules

D = Decimal

#: 所有序列的起点。用固定时刻而不是 `now()`：推算按**时刻差**算，跟今天是哪天
#: 无关，而写死能让失败信息里的数字对得上手算的结果。
_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _points(*offsets_and_qty: tuple[float, str]) -> list[rules.StockPoint]:
    """按 (距起点几天, 库存) 造一串快照点。"""
    return [
        rules.StockPoint(captured_at=_T0 + timedelta(days=days), qty=D(qty))
        for days, qty in offsets_and_qty
    ]


# ---- runway()：库存 ÷ 日均销量 ------------------------------------------------


@pytest.mark.parametrize(
    ("stock_qty", "avg_daily_sales", "expected_days"),
    [
        (D(100), D(10), D("10.0")),
        (D(70), D(10), D("7.0")),
        # 除不尽的按 0.1 天取整，同余额那条。
        (D(70), D(30), D("2.3")),
        # 按重量卖的品库存本来就带小数，全程 Decimal 不经 float。
        (D("12.5"), D("2.5"), D("5.0")),
    ],
)
def test_days_left_is_stock_over_daily_sales(
    stock_qty: Decimal,
    avg_daily_sales: Decimal,
    expected_days: Decimal,
) -> None:
    assert rules.runway(stock_qty, avg_daily_sales).days_left == expected_days


@pytest.mark.parametrize("avg_daily_sales", [None, D(0), D("-1")])
def test_no_recent_sales_is_undefined_and_never_alerts(avg_daily_sales: Decimal | None) -> None:
    """算不出日均、或者近期没动销 → 无定义，且**不告警**。

    这两种情况在出参里要能分开（`None` 是「算不出来」，`0` 是「真的没卖」），
    但在告警判定上是同一个结论：没有动销就不会断货，报一条只会让人对告警脱敏。
    """
    verdict = rules.runway(D(100), avg_daily_sales)
    assert verdict.days_left is None
    assert verdict.is_alerting is False


@pytest.mark.parametrize("stock_qty", [D(0), D("-3")])
def test_sold_out_is_zero_days_and_alerts(stock_qty: Decimal) -> None:
    """已经卖光（甚至超卖）→ 0 天且告警，**不返回负数**。"""
    verdict = rules.runway(stock_qty, D(10))
    assert verdict.days_left == D(0)
    assert verdict.is_alerting is True


def test_threshold_is_inclusive() -> None:
    """恰好等于阈值就该告警 —— 阈值的语义是「留给人补货的时间」。"""
    exactly = rules.runway(rules.ALERT_THRESHOLD_DAYS * 10, D(10))
    assert exactly.days_left == rules.ALERT_THRESHOLD_DAYS
    assert exactly.is_alerting is True

    just_over = rules.runway(rules.ALERT_THRESHOLD_DAYS * 10 + 1, D(10))
    assert just_over.is_alerting is False


def test_stock_threshold_is_longer_than_the_balance_one() -> None:
    """🔴 库存的阈值必须比余额的长。

    不是口味问题：余额告急之后是「通知客户 → 充值」（分钟级），库存告急之后是
    「通知客户 → 下单 → 生产/发货 → 入仓」（周级）。两个数调成一样，等于在货已经
    来不及补的时候才开始提醒 —— 告警照样准，但没用。

    哪天有人「统一一下这两个常量」，这条会拦住他。
    """
    from adpilot.rules import balance as balance_rules

    assert rules.ALERT_THRESHOLD_DAYS > balance_rules.ALERT_THRESHOLD_DAYS


# ---- infer_daily_sales()：从库存变化推日均 -----------------------------------


def test_inferred_sales_is_the_drop_over_the_span() -> None:
    """两点之间掉了多少，就是这段时间卖了多少。"""
    assert rules.infer_daily_sales(_points((0, "100"), (4, "60"))) == D(10)


def test_inferred_sales_spans_multiple_segments() -> None:
    """多段累加：总降幅 ÷ 总天数，而不是各段日均再平均。

    后者会让「一天掉 10」和「十天掉 10」在结果里权重一样，而它们代表的动销速度
    差着一个量级。
    """
    # 掉 20（2 天）+ 掉 40（4 天）= 60 / 6 天
    assert rules.infer_daily_sales(_points((0, "200"), (2, "180"), (6, "140"))) == D(10)


def test_flat_segments_count_toward_the_denominator() -> None:
    """🔴 库存没动的那几天**算进分母**，销量记 0。

    「三天没卖出去」是可信的信息，不是缺数据 —— 与余额那条「暂停投放的日子有指标
    行、spend 是 0，本来就该拉低日均」是同一条规矩的第二次落地。

    漏掉它的话，下面这串会算成 10/1 = 10（高估四倍），可撑天数被腰斩再腰斩，
    于是一批凭空多出来的断货告警。
    """
    # 三天没动，第四天掉了 10 → 10 件 / 4 天
    assert rules.infer_daily_sales(_points((0, "100"), (3, "100"), (4, "90"))) == D("2.5")


def test_restock_segments_are_skipped_entirely() -> None:
    """🔴 补货那一段整个跳过 —— 连天数都不算。

    「补了 100 卖了 20」和「补了 80 卖了 0」在两个端点上看**完全一样**，硬拆只能
    靠猜。把它的天数计入分母而销量记 0，等于断言「这段一件没卖」，那是拿猜测当
    数据。
    """
    # 第 0→2 天掉 20（算），第 2→4 天补货到 500（跳过），第 4→6 天掉 40（算）
    points = _points((0, "100"), (2, "80"), (4, "500"), (6, "460"))
    # (20 + 40) / (2 + 2) = 15
    assert rules.infer_daily_sales(points) == D(15)


def test_only_restocks_means_undefined() -> None:
    """所有段都被跳过 → 推不出来，而不是 0。

    返回 0 会让 `runway()` 判成「近期没卖，不告警」，两者结论碰巧一样；但出参里
    「算不出来」和「真的没卖」要采取的行动完全不同 —— 前者是「再导一次库存」。
    """
    assert rules.infer_daily_sales(_points((0, "50"), (2, "120"))) is None


def test_a_single_snapshot_is_not_enough() -> None:
    """只有一条快照当然推不出速度。页面据此提示「再导一次就能算了」。"""
    assert rules.infer_daily_sales(_points((0, "100"))) is None
    assert rules.infer_daily_sales([]) is None


def test_stale_segments_are_dropped() -> None:
    """🔴 跨度超过 `MAX_SPAN_DAYS` 的那一段不算。

    三个月前传过一次、今天又传一次，中间补了几轮、断了几次完全不知道 —— 两个端点
    相减除以 90 天，算出来的是个**精确的胡话**。宁可返回「无定义」。
    """
    span = float(rules.MAX_SPAN_DAYS) + 1
    assert rules.infer_daily_sales(_points((0, "1000"), (span, "100"))) is None

    # 同一串里，新鲜的那一段照样算 —— 丢的是过期的段，不是整个序列。
    points = _points((0, "1000"), (span, "100"), (span + 2, "80"))
    assert rules.infer_daily_sales(points) == D(10)


def test_points_may_arrive_in_any_order() -> None:
    """查询那边按 `captured_at` 倒序取（最新的在前），规则自己排回正序。

    不排的话，倒序序列的每一段都是「库存上升」，于是整串被当成补货全部跳过 ——
    结果是每个商品都「算不出来」，而且不会有任何报错。
    """
    ascending = _points((0, "100"), (4, "60"))
    assert rules.infer_daily_sales(list(reversed(ascending))) == D(10)


def test_duplicate_instants_do_not_divide_by_zero() -> None:
    """同一时刻的两条快照跨度是 0 —— 跳过，别让它变成除以 0。

    唯一约束挡住了同一个 (商品, 时刻) 落两行，但这个函数是纯函数，边界得自己守。
    """
    same_instant = [
        rules.StockPoint(captured_at=_T0, qty=D(100)),
        rules.StockPoint(captured_at=_T0, qty=D(90)),
    ]
    assert rules.infer_daily_sales(same_instant) is None
