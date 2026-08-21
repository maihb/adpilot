"""库存可撑天数：主推款断货前的最后一道提醒。

```
stock_days_left = stock_qty / avg_daily_sales
```

与余额那条是同一个形状，但**分母难得多**：余额的日均消耗从 `daily_metrics` 算得
出来，库存的日均销量在店铺后台、而且导出未必带这一列。所以这个模块除了那个除法，
还负责**从快照序列把日均销量推出来**（`infer_daily_sales`）。

口径见 [glossary](../../../docs/business/glossary.md) 的「库存可撑天数」，设计与
取舍见[库存断货设计](../../../docs/design/2026-08-21-stock-alerts.md)。这个模块
不查库：快照由 `services/product.py` 查好了送进来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

#: 一天有多少秒。快照之间的跨度是**时刻差**（不是自然日差）—— 库存快照什么时候
#: 传由人决定，早上传和晚上传差半天，按自然日算会把那半天算成一整天。
_SECONDS_PER_DAY = Decimal(86400)

# ⚠️ 下面三个参数都还没有业务上的定论（glossary 里记着「待定」），这里是当前取值。
# 定下来的那天只改这三行 —— 它们是这几个假设在整个代码库里的唯一落点。

#: 低于这个天数就告警。**7 天，不是余额那边的 3 天。**
#:
#: 同一个语义（「留给人反应的时间」），两条规则落成两个数，因为反应链路不是一回事：
#: 余额告急之后要做的是「通知客户 → 客户充值」，那是**分钟级**的动作；库存告急
#: 之后是「通知客户 → 客户下单 → 生产/发货 → 入仓」，那是**周级**的。设成 3 天
#: 等于在货已经来不及补的时候才开始提醒 —— 告警照样准，但没用。
ALERT_THRESHOLD_DAYS = Decimal(7)

#: 推日均销量时往回取几条快照。**取条数，不取天数**，这条和余额那边刻意不一样。
#:
#: 库存快照的时间密度由**人的导入节奏**决定，不由日历决定。硬按 7 天窗口切，
#: 「一周传一次」的客户永远只剩一个点、永远算不出日均 —— 而导得少的客户恰恰是
#: 盯得少的那批，最需要这条规则。8 条够覆盖「一天传一次」的一整周。
LOOKBACK_POINTS = 8

#: 相邻两条快照跨度超过这么多天，那一段整个不算。
#:
#: 三个月前传过一次、今天又传一次，中间发生过什么（补了几轮、断了几次）完全不知道，
#: 拿两个端点相减除以 90 天，算出来的是个**精确的胡话**。宁可返回「无定义」。
MAX_SPAN_DAYS = Decimal(30)

#: 天数保留几位小数。同余额那条：日报里没人需要「还能撑 2.3333333 天」。
_QUANTUM = Decimal("0.1")

#: 日均销量保留几位。它是件数，对齐 `models/product.py` 的 `QUANTITY` 列精度。
#: 在**产地**量化的理由同 `rules/balance.py`：这个值有两个出口（告警里的人话、
#: `detail` JSONB），留给调用方量化必漏一个。
_QTY_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class StockPoint:
    """一条库存快照上规则用得着的那两个字段。"""

    captured_at: datetime
    qty: Decimal


@dataclass(frozen=True, slots=True)
class StockRunway:
    """一个商品的库存还能撑多久。

    `days_left` 为 `None` 表示**无定义**，不是 0 也不是「永远够用」—— 近期没卖出
    去、或者快照不够推不出日均时，这个除法没有意义。此时 `is_alerting` 一定是
    `False`：没有消耗就不会断货，报一条只会让人对告警脱敏。

    `avg_daily_sales` 也可能是 `None`，而它和「日均是 0」是两件事：
    **`None` 是「算不出来」（只有一条快照），`0` 是「真的没卖」。** 前者要人去补
    数据，后者不用管，出参那边照直区分。
    """

    days_left: Decimal | None
    is_alerting: bool
    stock_qty: Decimal
    avg_daily_sales: Decimal | None
    threshold_days: Decimal = ALERT_THRESHOLD_DAYS


def runway(stock_qty: Decimal, avg_daily_sales: Decimal | None) -> StockRunway:
    """算库存可撑天数并判定要不要告警。

    四条边界，前三条与 `rules/balance.py` 的 `runway()` 一一对应（同一条规矩在
    两条规则上的两次落地），第四条是库存独有的：

    * **日均销量算不出来（`None`）→ 无定义，不告警。** 只有一条快照、或者中间只
      发生过补货。这与「没卖出去」不是一回事，见 `StockRunway` 的 docstring。
    * **日均销量为 0（或负）→ 无定义，不告警。** 这个款近期没动销，不会断货。
    * **库存 ≤ 0 → 0 天，且告警。** 已经卖光了，广告此刻正在给一个买不到的
      链接引流 —— 这是这条规则最该抓住的那一刻。不返回负数。
    * **判定用 `<=` 不是 `<`。** 恰好等于阈值就该告警：阈值的语义是「留给人补货
      的时间」，卡在边界上时那点时间已经不够了。
    """
    if avg_daily_sales is None or avg_daily_sales <= 0:
        return StockRunway(
            days_left=None,
            is_alerting=False,
            stock_qty=stock_qty,
            avg_daily_sales=avg_daily_sales,
        )

    if stock_qty <= 0:
        return StockRunway(
            days_left=Decimal(0),
            is_alerting=True,
            stock_qty=stock_qty,
            avg_daily_sales=avg_daily_sales,
        )

    days_left = (stock_qty / avg_daily_sales).quantize(_QUANTUM)
    return StockRunway(
        days_left=days_left,
        is_alerting=days_left <= ALERT_THRESHOLD_DAYS,
        stock_qty=stock_qty,
        avg_daily_sales=avg_daily_sales,
    )


def infer_daily_sales(points: Sequence[StockPoint]) -> Decimal | None:
    """没有现成的销量列时，从库存快照序列把日均销量推出来。

    思路一句话：**相邻两次快照之间库存掉了多少，就是这段时间卖了多少。**

    ```
    avg_daily_sales = Σ(相邻两点的库存降幅) / Σ(那些段覆盖的天数)
    ```

    三条边界，方向不一样，都得写清楚（[设计][d]第四节有对照表）：

    | 相邻两点 | 算不算 | 为什么 |
    |---|---|---|
    | 库存**下降** | ✅ 降幅计入销量，跨度计入天数 | 正常消耗 |
    | 库存**没动** | ✅ 销量记 0，**跨度照样计入天数** | 「三天没卖出去」是可信的信息，不是缺数据 |
    | 库存**上升** | ❌ 整段跳过 | 补货了。「补了 100 卖了 20」和「补了 80 卖了 0」
      在两个端点上看**完全一样** |

    中间那条是从余额那条规则原样搬过来的：那边「暂停投放的日子有指标行、`spend`
    是 0，本来就该拉低日均」，这边「没卖出去的日子库存没动，本来就该拉低日均」——
    **区分「没数据」和「没发生」，是这两条规则共用的那个分母的全部意义。** 漏掉它
    会让日均被系统性高估 → 可撑天数被低估 → 一批凭空多出来的断货告警。

    还有一条上限：**跨度超过 `MAX_SPAN_DAYS` 的相邻两点整段不算**，理由见那个常量。

    返回 `None` 表示**推不出来**（点不够两个、或者所有段都被跳过了），由 `runway()`
    判成「无定义」。注意它和返回 `0` 不是一回事，后者是「真的没卖」。

    [d]: ../../../docs/design/2026-08-21-stock-alerts.md
    """
    # 不信调用方给的顺序：这个函数在测试里会被直接喂一串手写的点，而查询那边的
    # 排序（最近 N 条 → 倒序）和这里要的（时间正序）恰好相反。排一次很便宜。
    ordered = sorted(points, key=lambda point: point.captured_at)

    total_sold = Decimal(0)
    total_days = Decimal(0)
    for previous, current in pairwise(ordered):
        span_seconds = Decimal((current.captured_at - previous.captured_at).total_seconds())
        span_days = span_seconds / _SECONDS_PER_DAY
        if span_days <= 0 or span_days > MAX_SPAN_DAYS:
            continue

        sold = previous.qty - current.qty
        if sold < 0:
            # 补货。这一段说不清卖了多少，连天数也不能算 —— 把它的天数计入分母
            # 而销量记 0，等于断言「这段时间一件没卖」，那是在拿一个猜测当数据。
            continue

        total_sold += sold
        total_days += span_days

    if total_days <= 0:
        return None
    return (total_sold / total_days).quantize(_QTY_QUANTUM)
