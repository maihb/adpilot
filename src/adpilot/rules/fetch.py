"""数据停更判定：自动拉取还在不在正常工作。

**纯函数，不碰 IO**（同 `rules/` 下另外三个模块）：查库和对账在 `services/`，
这里只回答「给定这些数，该不该报」。

## 为什么这条规则和另外三条不是一类

余额、指标异动、库存断货说的都是「**数据显示**出了问题」。这一条说的是
「**数据本身**可能是假的」—— 拉取一停，看板上的花费就变成 0，而 0 花费和
「昨天没投放」在每一屏上都长得一模一样：余额告警会因为没有新消耗而安静下来，
日报照常生成，所有数字看起来都很正常。

所以它是**元规则**：它成立的时候，另外三条的结论全都不可信。
（[设计文档第三节](../../../docs/design/2026-08-25-ads-api-fetch.md)）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FetchTrouble(StrEnum):
    """停更的两种形态。**处置不同，所以要分开。**"""

    #: 试过，报错了。人要去看那条 `last_error` —— 多半是 token 失效或权限不对。
    FAILING = "failing"

    #: 没报错，但太久没成功过了。**这一种更危险**：没有任何一条错误信息，只有
    #: 一个越来越旧的时间戳。典型成因是排期任务根本没在跑（beat 没起、worker
    #: 死了、账户被从扫描范围里漏掉了），而那些故障不会经过任何一条 except。
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FetchHealth:
    """一个账户的拉取健康度。`is_alerting` 为真就是要报的那些。"""

    trouble: FetchTrouble | None
    hours_since_success: int | None
    consecutive_failures: int

    @property
    def is_alerting(self) -> bool:
        return self.trouble is not None


def evaluate(
    *,
    last_success_at: datetime | None,
    consecutive_failures: int,
    now: datetime,
    stale_hours: int,
    min_failures: int = 2,
) -> FetchHealth:
    """判断这个账户的自动拉取是不是出问题了。

    ### `min_failures` 默认 2，不是 1

    一次失败可能是平台抖了一下 —— 而任务本身会退避重试，多半在同一轮里就自愈了。
    连着两轮（即两小时）都不成功才说明真的坏了。**这不是容忍故障，是让告警值得
    被相信**：一条每周误报几次的告警，三周之后就没人看了。

    ⚠️ 但**「连续失败」和「太久没成功」是或的关系**，不是与。失败计数为 0 也可能
    停更（任务压根没跑），那种情况恰恰最危险，见 `FetchTrouble.STALE`。

    ### 从来没成功过怎么算

    `last_success_at is None` 且已经试过（有失败计数）→ 按 `FAILING` 报。**没试过
    也没失败过的账户根本不会有 `fetch_states` 行**，调用方压根不会走到这里 ——
    刚挂上凭据、还没到第一个排期点的账户不该因此报警。
    """
    hours = None if last_success_at is None else _hours_between(last_success_at, now)

    if consecutive_failures >= min_failures:
        return FetchHealth(
            trouble=FetchTrouble.FAILING,
            hours_since_success=hours,
            consecutive_failures=consecutive_failures,
        )

    # 从没成功过、且失败次数还没到阈值：给它把「多久没成功」算成「从有这行记录
    # 到现在」是做不到的（这层拿不到 created_at，那是 IO）。所以按 STALE 处理由
    # 上面那个分支之外的这一条兜住 —— hours 为 None 时不报，等失败计数攒够。
    if hours is not None and hours >= stale_hours:
        return FetchHealth(
            trouble=FetchTrouble.STALE,
            hours_since_success=hours,
            consecutive_failures=consecutive_failures,
        )

    return FetchHealth(
        trouble=None,
        hours_since_success=hours,
        consecutive_failures=consecutive_failures,
    )


def _hours_between(earlier: datetime, later: datetime) -> int:
    """两个时刻差几个整小时，**向下取整、不给负数**。

    时钟回拨、或者一条时间戳来自另一台机器时，差值可以是负的。让它变成 0 而不是
    负数：一个「-3 小时没成功」的告警文案会让人怀疑整套系统，而实际含义就是
    「刚刚成功过」。
    """
    delta = later - earlier
    return max(0, int(delta.total_seconds() // 3600))
