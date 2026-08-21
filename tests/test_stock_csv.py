"""库存表解析的单元测试。不碰任何外部服务。

**这一层的正确性和 `test_csv_provider.py` 一样值得钉死**，而且方向更危险：库存
认错了不会报错，只会让可撑天数算成另一个数 —— 而「还能撑 12 天」和「还能撑 1.2
天」在页面上长得一样正常。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adpilot.providers.base import ParseError
from adpilot.providers.stock_csv import StockCsvParser


def _csv(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


def test_columns_are_recognised_by_synonym() -> None:
    """中文列名、英文列名都要认得出 —— 店铺后台按界面语言导出。"""
    result = StockCsvParser().parse(
        _csv(
            "商品编码,商品名称,可售库存,日均销量",
            "A-001,夏季连衣裙,120,8",
        )
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.sku == "A-001"
    assert row.name == "夏季连衣裙"
    assert row.qty == Decimal(120)
    assert row.daily_sales == Decimal(8)


def test_the_sales_column_is_optional() -> None:
    """🔴 没有日均销量列时 `daily_sales` 是 `None`，**不是 0**。

    读成 0 会让 `runway()` 判成「近期没卖，不告警」—— 于是每个用「只有编码和库存
    两列」那种导出的客户，断货告警一条都不会来，而且不会有任何报错。`None` 才会
    走到「从快照序列推」那条路上。
    """
    result = StockCsvParser().parse(_csv("sku,stock", "A-001,120"))
    assert result.rows[0].daily_sales is None
    assert result.rows[0].name is None


def test_an_empty_sales_cell_is_also_none() -> None:
    """有那一列、但这一行没填，同样是「算不出来」而不是「没卖」。"""
    result = StockCsvParser().parse(
        _csv(
            "sku,stock,日均销量",
            "A-001,120,",
            "A-002,80,4",
        )
    )
    assert result.rows[0].daily_sales is None
    assert result.rows[1].daily_sales == Decimal(4)


def test_missing_required_column_lists_the_headers() -> None:
    """认不出必填列时**把表头列出来** —— 只说「格式不对」等于没说。"""
    with pytest.raises(ParseError) as excinfo:
        StockCsvParser().parse(_csv("编码,数量", "A-001,120"))

    assert "表头" in excinfo.value.message
    assert "数量" in excinfo.value.message


def test_thousands_separators_and_units_are_tolerated() -> None:
    """「1,240 件」是常见的导出形态，而 `Decimal("1,240")` 直接抛异常。"""
    result = StockCsvParser().parse(
        _csv(
            "sku,库存",
            'A-001,"1,240"',
            "A-002,86 件",
        )
    )
    assert result.rows[0].qty == Decimal(1240)
    assert result.rows[1].qty == Decimal(86)


def test_oversold_is_clamped_to_zero() -> None:
    """负库存（超卖）夹到 0：要采取的行动和「卖光了」完全一样，而放它进来会让
    可撑天数变成负数。"""
    result = StockCsvParser().parse(_csv("sku,库存", "A-001,-3"))
    assert result.rows[0].qty == Decimal(0)


def test_rows_without_a_sku_are_skipped_and_counted() -> None:
    """末尾的合计行没有编码。跳过，但把数字报出来 —— 静默丢掉会让「导进去的条数
    对不上」变成一桩无头案。"""
    result = StockCsvParser().parse(
        _csv(
            "sku,库存",
            "A-001,120",
            ",300",
            "A-002,80",
        )
    )
    assert [row.sku for row in result.rows] == ["A-001", "A-002"]
    assert result.skipped_rows == 1


def test_a_duplicate_sku_is_an_error_not_a_guess() -> None:
    """🔴 同一份文件里同一个编码出现两次 → 报错，**不猜**。

    可能是按仓库分行（该相加）、也可能是复制粘贴出的重复（该去重），两种处理得出
    的库存差着一倍，而它们在文件里长得一模一样。报错时带上两处的行号。
    """
    with pytest.raises(ParseError) as excinfo:
        StockCsvParser().parse(
            _csv(
                "sku,库存",
                "A-001,120",
                "A-002,80",
                "A-001,40",
            )
        )

    assert "A-001" in excinfo.value.message
    assert "第 2 行" in excinfo.value.message


def test_empty_stock_is_rejected() -> None:
    """库存这一格空着不能读成 0 —— 那是「没导出来」，不是「卖光了」。"""
    with pytest.raises(ParseError):
        StockCsvParser().parse(_csv("sku,库存", "A-001,"))


def test_non_numeric_stock_points_at_the_line() -> None:
    with pytest.raises(ParseError) as excinfo:
        StockCsvParser().parse(_csv("sku,库存", "A-001,充足"))

    assert "第 2 行" in excinfo.value.message


def test_bom_does_not_break_column_detection() -> None:
    """Excel 另存的 CSV 带 BOM，用普通 utf-8 解码会让第一个表头变成 "﻿sku"，
    而那个字符在报错信息里是**看不见的**。"""
    content = "﻿sku,库存\nA-001,120\n".encode()
    assert StockCsvParser().parse(content).rows[0].sku == "A-001"


def test_a_header_only_file_is_an_error() -> None:
    with pytest.raises(ParseError):
        StockCsvParser().parse(_csv("sku,库存"))
