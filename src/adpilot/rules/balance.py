"""余额可撑天数：预充账户归零前的最后一道提醒。

```
days_left = available_balance / avg_daily_spend
```

**为什么这条排在所有规则最前面**：TikTok 预充模式下余额归零，广告是**停**不是降速
（平台原文 `Your ad spend will be automatically deducted from your available cash
balance`）。停一次，3–5 天的学习期白跑，重开还要再等 3–5 天 —— 代价远大于「少花
一天钱」。

口径见 [glossary](../../../docs/business/glossary.md) 的「余额可撑天数」。这个模块
不查库：`available` 和 `avg_daily_spend` 由 `services/balance.py` 查好了送进来。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# ⚠️ **两个参数都还没有业务上的定论**（glossary 里记着「待定」），下面是当前取值。
# 定下来的那天只改这两行 —— 它们是这两个假设在整个代码库里的唯一落点。

#: 算日均消耗时往回看几天。7 天盖住一个完整的周内/周末周期 —— 广告花费的周节律
#: 很明显，取 3 天会让「周一算出来的日均」系统性偏低。
LOOKBACK_DAYS = 7

#: 低于这个天数就告警。3 天是「发现 → 通知客户 → 客户走完充值流程」的现实耗时；
#: 卡到 1 天，等消息传到能付钱的人手里，广告已经停了。
ALERT_THRESHOLD_DAYS = Decimal(3)

#: 除法保留几位小数。天数给到 0.1 天就够用了，多出来的位数只会让日报里出现
#: 「还能撑 2.3333333 天」这种没人需要的精度。
_QUANTUM = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class BalanceRunway:
    """一个账户的余额还能撑多久。

    `days_left` 为 `None` 表示**无定义**，不是 0 也不是无穷 —— 近期没花钱时这个
    除法没有意义，此时 `is_alerting` 一定是 `False`：没有消耗就不会归零，报一条
    「余额告急」只会让人对告警脱敏。
    """

    days_left: Decimal | None
    is_alerting: bool
    available: Decimal
    avg_daily_spend: Decimal
    threshold_days: Decimal = ALERT_THRESHOLD_DAYS


def runway(available: Decimal, avg_daily_spend: Decimal) -> BalanceRunway:
    """算可撑天数并判定要不要告警。

    三条边界，每条都对应一种「不写清楚就会错」的情况：

    * **日均消耗为 0（或负）→ 无定义，不告警。** 账户暂停投放、或者这段时间根本
      没导数据。分母为 0 时返回 0 会把它显示成「立刻要停」，返回无穷会显示成
      「永远够用」，两个都是谎。这条与 glossary 里「分母为 0 一律返回 null」是
      同一条规矩。
    * **余额 ≤ 0 → 0 天，且告警。** 广告此刻就是停的。这里不返回负数：负的
      「可撑天数」没有意义，而 0 已经把「该充钱了」说得足够清楚。
    * **判定用 `<=` 不是 `<`。** 恰好等于阈值就该告警 —— 阈值的语义是「留给人
      反应的时间」，卡在边界上时那点时间已经不够了。
    """
    if avg_daily_spend <= 0:
        return BalanceRunway(
            days_left=None,
            is_alerting=False,
            available=available,
            avg_daily_spend=avg_daily_spend,
        )

    if available <= 0:
        return BalanceRunway(
            days_left=Decimal(0),
            is_alerting=True,
            available=available,
            avg_daily_spend=avg_daily_spend,
        )

    days_left = (available / avg_daily_spend).quantize(_QUANTUM)
    return BalanceRunway(
        days_left=days_left,
        is_alerting=days_left <= ALERT_THRESHOLD_DAYS,
        available=available,
        avg_daily_spend=avg_daily_spend,
    )


def average_daily_spend(total_spend: Decimal, days_with_data: int) -> Decimal:
    """把「区间内的总花费」摊成日均。

    🔴 **分母是「区间内真有数据的天数」，不是回看窗口的 `LOOKBACK_DAYS`。**

    差别在缺数据的时候，而且错的方向很糟：某天没导入 ≠ 那天没花钱，用 7 当分母会
    让日均被系统性拉低 → 可撑天数被高估 → **该告警的账户不告警**。反过来（用实际
    天数）最坏只是高估日均、多报一次告警，那个方向的代价小得多。

    真正暂停投放的日子仍然会被算进去：那些天有指标行、`spend` 是 0，本来就该拉低
    日均。区分「没数据」和「没花钱」正是这个分母的全部意义。

    一天数据都没有时返回 0，由 `runway()` 判成「无定义」。
    """
    if days_with_data <= 0:
        return Decimal(0)
    return total_spend / days_with_data
