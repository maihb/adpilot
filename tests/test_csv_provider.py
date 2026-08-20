"""CSV 解析的单元测试。不碰任何外部服务。

**这一层的正确性特别值得钉死**：解析错了通常不会报错，只会让数据挂到错的日期上，
而报表看起来完全正常 —— 这类错误要等客户拿自己后台的数字来对才会暴露。
"""

from __future__ import annotations

from datetime import date

import pytest

from adpilot.providers.base import ParseError
from adpilot.providers.csv_file import CsvImportProvider


def _csv(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


def test_rows_are_grouped_by_day() -> None:
    """快照按 (账户, 日期) 分文档存，所以解析的产物必须先按天分好组。"""
    result = CsvImportProvider().parse(
        _csv(
            "Day,Campaign,Spend",
            "2026-08-18,cmp-1,12.34",
            "2026-08-18,cmp-2,56.78",
            "2026-08-19,cmp-1,90.12",
        )
    )

    assert [day.stat_date for day in result.days] == [date(2026, 8, 18), date(2026, 8, 19)]
    assert len(result.days[0].rows) == 2
    assert len(result.days[1].rows) == 1
    assert result.skipped_rows == 0


def test_column_names_are_kept_verbatim() -> None:
    """🔴 这一层**一个字段都不映射**。

    raw_reports 存的必须是未经解释的原始行 —— 映射规则改了或者发现 bug，要能拿
    这些行重跑。提前把 "Amount spent (USD)" 改名成 spend，就等于把当时的映射规则
    永久烧进了快照，重跑也救不回来。
    """
    result = CsvImportProvider().parse(
        _csv(
            "Day,Amount spent (USD),Link clicks",
            "2026-08-18,12.34,5",
        )
    )

    assert result.days[0].rows[0] == {
        "Day": "2026-08-18",
        "Amount spent (USD)": "12.34",
        "Link clicks": "5",
    }


def test_bom_does_not_break_header_detection() -> None:
    """Excel 另存的 CSV 带 BOM，用普通 utf-8 解码会让第一个表头变成 "﻿Day"。

    值得单独一条测试，是因为这个失败**看不见**：报错信息里那个 BOM 不显示，
    人对着两个一模一样的字符串查半天。
    """
    result = CsvImportProvider().parse("﻿Day,Spend\n2026-08-18,1".encode())

    assert result.days[0].stat_date == date(2026, 8, 18)


@pytest.mark.parametrize(
    "header",
    ["Day", "Date", "日期", "stat_time_day", "Reporting starts"],
)
def test_known_date_columns_are_detected(header: str) -> None:
    """各家后台导出的日期列叫法不同，常见的这几种要能自动认出来。"""
    result = CsvImportProvider().parse(_csv(f"{header},Spend", "2026-08-18,1"))

    assert result.days[0].stat_date == date(2026, 8, 18)


def test_missing_date_column_reports_the_actual_headers() -> None:
    """认不出日期列时要把**实际表头**列出来。

    只说「找不到日期列」的话，导入的人根本不知道该拿什么去填 date_column ——
    尤其列名可能带着看不见的空格或 BOM。
    """
    with pytest.raises(ParseError) as caught:
        CsvImportProvider().parse(_csv("Campaign,Spend", "cmp-1,1"))

    assert "Campaign" in caught.value.message


def test_explicit_date_column_overrides_detection() -> None:
    """两列都像日期时，显式指定的那个说了算。"""
    result = CsvImportProvider(date_column="Reporting starts").parse(
        _csv(
            "Day,Reporting starts,Spend",
            "2026-08-18,2026-08-01,1",
        )
    )

    assert result.days[0].stat_date == date(2026, 8, 1)


def test_explicit_date_column_must_exist() -> None:
    with pytest.raises(ParseError) as caught:
        CsvImportProvider(date_column="没这列").parse(_csv("Day,Spend", "2026-08-18,1"))

    assert "没这列" in caught.value.message


def test_total_row_is_skipped_and_counted() -> None:
    """平台导出末尾那行「Total」没有日期。

    跳过它但把数字报出来：直接报错会让每次导入都得先手工删一行，静默丢掉又会让
    「行数对不上」变成无头案。
    """
    result = CsvImportProvider().parse(
        _csv(
            "Day,Campaign,Spend",
            "2026-08-18,cmp-1,10",
            ",Total,100",
        )
    )

    assert result.skipped_rows == 1
    assert len(result.days) == 1
    assert len(result.days[0].rows) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-18", date(2026, 8, 18)),
        ("2026/08/18", date(2026, 8, 18)),
        ("20260818", date(2026, 8, 18)),
    ],
)
def test_accepted_date_formats(value: str, expected: date) -> None:
    result = CsvImportProvider().parse(_csv("Day,Spend", f"{value},1"))

    assert result.days[0].stat_date == expected


def test_ambiguous_date_is_rejected_with_a_line_number() -> None:
    """🔴 `03/04/2026` 在美式和欧式下是两个不同的日期，一律拒绝而**不猜**。

    宽容的解析器会安静地替你选一个，而错了一天的数据在报表里同样看不出来。
    报错必须带行号 —— 几千行的文件里没有行号无从下手。
    """
    with pytest.raises(ParseError) as caught:
        CsvImportProvider().parse(_csv("Day,Spend", "2026-08-18,1", "03/04/2026,2"))

    assert "第 3 行" in caught.value.message


def test_row_with_more_columns_than_header_is_rejected() -> None:
    """列数比表头多，说明文件被改坏了，不能当没看见。

    DictReader 会把多出来的值塞进一个额外的键，而**Mongo 的文档键只能是字符
    串** —— 放任它流下去，写入会在完全无关的地方失败。
    """
    with pytest.raises(ParseError) as caught:
        CsvImportProvider().parse(_csv("Day,Spend", "2026-08-18,1,多出来的"))

    assert "第 2 行" in caught.value.message


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ParseError):
        CsvImportProvider().parse(b"")


def test_header_only_file_is_rejected() -> None:
    """只有表头说明导出时选错了范围，报错比落一条空快照有用。"""
    with pytest.raises(ParseError):
        CsvImportProvider().parse(_csv("Day,Spend"))


def test_non_utf8_content_is_rejected_with_guidance() -> None:
    """报错要说清怎么办，而不是甩一个 UnicodeDecodeError。"""
    with pytest.raises(ParseError) as caught:
        CsvImportProvider().parse("Day,Spend\n2026-08-18,1".encode("utf-16"))

    assert "UTF-8" in caught.value.message
