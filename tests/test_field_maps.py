"""列名映射与数值解析。不碰任何外部服务。

映射错了不会报错 —— 只会让某一列的数字落到另一个指标上，而报表照常渲染。所以
这一层的每条规则都值得单独钉死。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adpilot.models.daily_metric import MetricLevel
from adpilot.services.field_maps import (
    CLICKS_COLUMNS,
    OBJECT_ID_COLUMNS,
    OBJECT_NAME_COLUMNS,
    SPEND_COLUMNS,
    canonical,
    find_column,
    parse_decimal,
    parse_int,
)


def test_qualifier_in_parentheses_is_kept() -> None:
    """🔴 `Clicks (link)` 和 `Clicks (all)` 必须落到两个不同的键上。

    「顺手把括号内容一起去掉」是这里最自然、也最致命的写法：两列会撞成同一个键，
    而这两个指标能差好几倍。
    """
    assert canonical("Clicks (link)") != canonical("Clicks (all)")


def test_case_spacing_and_separators_are_ignored() -> None:
    """同一个指标在各语言版本、各导出版本间的写法不稳定。"""
    assert canonical("Link clicks") == canonical("link_clicks") == canonical("LinkClicks")


def test_link_clicks_wins_over_all_clicks() -> None:
    """🔴 两列都在时必须取链接点击（glossary「指标」一节）。

    「全部点击」把点赞、展开、看主页都算进去，用它算出来的 CPC 会好看得离谱 ——
    而那个数字会被写进发给客户的日报。
    """
    row = {"Clicks (all)": "500", "Clicks (link)": "50", "Day": "2026-08-18"}

    assert find_column(row, CLICKS_COLUMNS) == "Clicks (link)"


def test_all_clicks_is_used_when_link_clicks_is_absent() -> None:
    """只有全部点击时还是要能出数，退化而不是报错。"""
    assert find_column({"Clicks (all)": "500"}, CLICKS_COLUMNS) == "Clicks (all)"


@pytest.mark.parametrize("column", ["Amount spent (USD)", "Amount spent (CNY)", "Amount Spent"])
def test_currency_suffix_does_not_break_spend_matching(column: str) -> None:
    """花费列名带着账户币种后缀，随账户变 —— 所以匹配用前缀而不是全等。"""
    assert find_column({column: "1"}, SPEND_COLUMNS) == column


def test_object_id_columns_differ_by_level() -> None:
    """导「广告系列」层级和导「广告」层级，对象 ID 是不同的列。"""
    row = {"Campaign ID": "cmp-1", "Ad ID": "ad-9"}

    assert find_column(row, OBJECT_ID_COLUMNS[MetricLevel.CAMPAIGN]) == "Campaign ID"
    assert find_column(row, OBJECT_ID_COLUMNS[MetricLevel.AD]) == "Ad ID"


def test_missing_column_returns_none() -> None:
    assert find_column({"Day": "2026-08-18"}, SPEND_COLUMNS) is None


def test_decimal_keeps_full_precision() -> None:
    """🔴 `Decimal(字符串)`，绝不经 float 中转。

    经 float 的话 1234.5678 会在某一位上开始漂，而广告花费错一分钱，这个系统
    输出的所有数字就都不可信了。
    """
    assert parse_decimal("1234.5678") == Decimal("1234.5678")
    assert parse_decimal("0.1") + parse_decimal("0.2") == Decimal("0.3")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", Decimal("1234.56")),
        ("$1,234.56", Decimal("1234.56")),
        ("¥1,234.56", Decimal("1234.56")),
        (" 12.34 ", Decimal("12.34")),
    ],
)
def test_dirty_number_formats(raw: str, expected: Decimal) -> None:
    """千分位和货币符号在导出文件里都很常见。"""
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "--", "—", "N/A", "null", "无", None])
def test_platform_placeholders_become_zero(raw: object) -> None:
    """平台表示「这格没数据」的写法有一小撮，都按 0 处理。

    这在语义上是有损的，但对计数列是安全的：没有展示就是没有展示。真正要小心的
    是分母为 0 的派生指标 —— 那些一律现算，且按 glossary 返回「无定义」。
    """
    assert parse_decimal(raw) == Decimal(0)


def test_garbage_is_rejected_not_silently_zeroed() -> None:
    """认不出的内容要抛，不能悄悄当成 0 —— 那会让一整列数据静默消失。"""
    with pytest.raises(ValueError, match="不是一个数"):
        parse_decimal("大约一千")


def test_int_accepts_trailing_zero_decimals() -> None:
    """平台偶尔把计数导成 `1,234.0`，直接 int() 会在小数点上炸。"""
    assert parse_int("1,234.0") == 1234
    assert parse_int("1234") == 1234


def test_tiktok_api_dimension_names_are_recognised() -> None:
    """TikTok API 的维度名要能被认出来 —— **每一层都要**。

    D19 实跑逮到的：账户级的候选里少了 `advertiser_id`，于是账户层的快照一归一化
    就报「找不到对象 ID 列」。单测发现不了 —— provider 那边的用例只验产物形状，
    验不到下游读不读得懂。

    `advertiser_id` 和 AD 层的 `adid` **不会撞**：前缀匹配下 "advertiserid" 不以
    "adid" 开头。这条把那件事也钉住了。
    """
    rows = {
        MetricLevel.ACCOUNT: {"advertiser_id": "7001", "advertiser_name": "示例账户"},
        MetricLevel.CAMPAIGN: {"campaign_id": "c1", "campaign_name": "示例系列"},
        MetricLevel.ADGROUP: {"adgroup_id": "g1", "adgroup_name": "示例组"},
        MetricLevel.AD: {"ad_id": "a1", "ad_name": "示例广告"},
    }

    for level, row in rows.items():
        id_column = find_column(row, OBJECT_ID_COLUMNS[level])
        name_column = find_column(row, OBJECT_NAME_COLUMNS[level])

        assert id_column is not None, f"{level} 认不出对象 ID 列"
        assert name_column is not None, f"{level} 认不出对象名列"
        assert id_column != name_column
