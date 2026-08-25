"""平台导出的列名 → 统一口径。

**这是字段映射的唯一收口点。** 口径本身的真相源是
[glossary](../../../docs/business/glossary.md) 的「指标」一节；这里只负责「平台
把它叫什么」。平台会改列名、会出新的语言版本，改动全部落在这个文件里 —— 散到
各处的话，加一个语言版本就要满仓库找。

数值解析一律 `Decimal(原始字符串)`，**不经 float 中转**（CLAUDE.md 硬规矩 5）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from adpilot.models.daily_metric import MetricLevel


def canonical(name: str) -> str:
    """把列名规范成一个可比对的键：去掉空格、下划线、连字符、括号，转小写。

    🔴 **括号里的内容保留，只去掉括号本身。** `Clicks (link)` 和 `Clicks (all)`
    是两个不同的指标，连括号内容一起删掉会让它们撞成同一个键 —— 而我们恰恰要
    区分（glossary：固定取链接点击，两种口径的数字能差好几倍）。

    代价是币种后缀也进了键（`Amount spent (USD)` → `amountspentusd`），所以下面
    的匹配用**前缀**而不是全等。
    """
    return re.sub(r"[\s_\-()（）\[\]]+", "", name).strip().lower()


def find_column(row: Mapping[str, Any], candidates: Sequence[str]) -> str | None:
    """按候选顺序找列，返回**原始列名**；一个都没有就返回 None。

    候选顺序即优先级，前缀匹配。`Clicks (link)` 与 `Clicks (all)` 同时存在时，
    把 `clickslink` 排在 `clicks` 前面就能确保取到前者。
    """
    normalized = [(canonical(name), name) for name in row]
    for candidate in candidates:
        for key, original in normalized:
            if key.startswith(candidate):
                return original
    return None


# --- 维度列 -----------------------------------------------------------------
#
# 按 level 取不同的列：导「广告系列」层级的报表时对象是 campaign，导「广告」层级
# 时是 ad。level 由导入时显式给定，不从内容推断（理由见 services/imports.py）。

OBJECT_ID_COLUMNS: dict[MetricLevel, tuple[str, ...]] = {
    # `advertiserid` 是 TikTok API 账户级的维度名（D19 实跑逮到的：少了它，
    # 账户层的快照归一化时报「找不到对象 ID 列」）。**不会和 AD 层的 `adid`
    # 撞**：前缀匹配下 "advertiserid" 不以 "adid" 开头。
    MetricLevel.ACCOUNT: ("accountid", "adaccountid", "advertiserid", "广告账户id", "账户id"),
    MetricLevel.CAMPAIGN: ("campaignid", "广告系列id", "广告计划id"),
    MetricLevel.ADGROUP: ("adsetid", "adgroupid", "广告组id"),
    MetricLevel.AD: ("adid", "广告id", "创意id"),
}

OBJECT_NAME_COLUMNS: dict[MetricLevel, tuple[str, ...]] = {
    MetricLevel.ACCOUNT: ("accountname", "advertisername", "广告账户名称", "账户名称"),
    MetricLevel.CAMPAIGN: ("campaignname", "广告系列名称", "广告计划名称"),
    MetricLevel.ADGROUP: ("adsetname", "adgroupname", "广告组名称"),
    MetricLevel.AD: ("adname", "广告名称", "创意名称"),
}

# --- 指标列 -----------------------------------------------------------------

SPEND_COLUMNS = ("amountspent", "spend", "cost", "totalcost", "花费", "消耗")

IMPRESSIONS_COLUMNS = ("impressions", "impression", "展示次数", "展示量", "曝光量")

# 🔴 **固定取链接点击**，不是「全部点击」（glossary「指标」一节）。两者的差距能
# 到几倍 —— 全部点击把点赞、展开、看主页都算进去，用它算出来的 CPC 会好看得离谱。
# 候选顺序保证两列都在时取到链接点击。
CLICKS_COLUMNS = (
    "clickslink",
    "linkclicks",
    "链接点击量",
    "链接点击数",
    "clicksall",
    "clicks",
    "点击量",
)

# ⚠️ **取哪个转化事件必须显式配置**，glossary 里这条标着「待定」。配置项落地之前，
# 这里取平台给的 Results / 转化数 —— 也就是导出时在后台选定的那个事件。
#
# 这个假设**收口在这一个常量上**：口径定下来的那天只改这里，不必去追散在各处的
# 判断。同时 glossary 里记着现在取的是什么。
CONVERSIONS_COLUMNS = ("results", "conversions", "conversion", "转化数", "转化量")

REVENUE_COLUMNS = (
    "purchaseconversionvalue",
    "conversionvalue",
    "totalcompleteorderrevenue",
    "revenue",
    "转化价值",
    "购物价值",
)

REACH_COLUMNS = ("reach", "覆盖人数", "触达人数")

# 平台用来表示「这一格没有数据」的写法。它们**不是 0** 在语义上，但落库时按 0
# 处理是安全的：没有展示就是没有展示。真正要小心的是分母为 0 的派生指标，那些
# 一律现算、且按 glossary 返回「无定义」，不在这里存。
_NOT_A_NUMBER = frozenset({"", "-", "--", "—", "n/a", "na", "null", "none", "无"})


def parse_decimal(raw: object) -> Decimal:
    """把导出文件里的数字变成 `Decimal`。

    **绝不经过 float。** `Decimal(str)` 而不是 `Decimal(float(str))` —— 后者在
    1234.5678 这种值上就开始漂，而广告花费错一分钱，这个系统输出的所有数字就都
    不可信了。

    要处理的脏数据比想象中多：千分位逗号、货币符号、以及平台表示「无数据」的
    那一小撮写法。
    """
    text = str(raw).strip() if raw is not None else ""
    if text.lower() in _NOT_A_NUMBER:
        return Decimal(0)

    cleaned = re.sub(r"[,\s$¥€£%]", "", text)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"不是一个数：{text!r}") from exc


def parse_int(raw: object) -> int:
    """展示、点击这类整数计数。

    先走 `Decimal` 再取整，是因为平台偶尔会导出 `1,234.0` 这种形态；直接 `int()`
    会在小数点上炸，而那个报错跟「这是个计数列」之间毫无线索。
    """
    return int(parse_decimal(raw))
