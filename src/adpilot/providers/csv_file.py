"""平台后台导出的 CSV。

MVP 的唯一数据源（[设计文档第四节](../../../docs/design/2026-08-19-mvp-design.md)
写了为什么不先接 Ads API）。**列名原样保留，这一层一个字段都不映射** —— 唯一
需要认识的列是日期，因为快照按 (账户, 日期) 分文档存，不知道日期就没法分组。
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from adpilot.providers.base import ParseError, ParseResult, RawRows

# 各家后台导出的日期列叫法。Meta 导出是 Day 或 Reporting starts，TikTok 是 Date
# 或 stat_time_day，中文界面导出的是「日期」。
#
# 探测不到就报错，**不猜**。猜错的后果是整份数据挂到错的日期上，而报表里看起来
# 完全正常 —— 这类错误只有等客户来对数字才会暴露。
DATE_COLUMN_CANDIDATES = ("Day", "Date", "日期", "stat_time_day", "Reporting starts")

# 只认这三种格式。不引 dateutil 那类「什么都能解析」的库是刻意的：`03/04/2026`
# 在美式和欧式下是两个不同的日期，宽容的解析器会安静地替你选一个，而错了一天的
# 数据在报表里同样看不出来。要支持新格式就往这里加一条，别去放宽解析。
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")

# 行比表头长时，多出来的值会被 DictReader 塞进这个键。必须显式给一个名字：
# 默认是 None，而 **Mongo 的文档键只能是字符串**，None 键会让写入当场失败，
# 报错信息里也完全看不出是 CSV 列数不齐导致的。
_EXTRA_KEY = "_extra_columns"


class CsvImportProvider:
    """把一份导出的 CSV 解析成按天分组的原始行。"""

    name = "file_csv"

    def __init__(self, date_column: str | None = None) -> None:
        """`date_column` 显式指定日期列；不给就按 `DATE_COLUMN_CANDIDATES` 探测。"""
        self._date_column = date_column

    def parse(self, content: bytes) -> ParseResult:
        # utf-8-sig：Excel 另存的 CSV 常带 BOM，用普通 utf-8 解码会让第一个表头
        # 变成 "﻿Day"，日期列探测于是失败 —— 而报错信息里那个 BOM 是**看不
        # 见的**，对着一模一样的两个字符串查半天。
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("文件不是 UTF-8 编码，导出时请选 UTF-8（或先转码）") from exc

        reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA_KEY)
        if reader.fieldnames is None:
            raise ParseError("这个 CSV 是空的，连表头都没有")

        date_column = self._resolve_date_column(list(reader.fieldnames))

        grouped: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
        skipped = 0
        for line_number, raw_row in enumerate(reader, start=2):  # 第 1 行是表头
            row = dict(raw_row)
            if _EXTRA_KEY in row:
                raise ParseError(
                    f"第 {line_number} 行的列数比表头多，文件可能被改坏了"
                    f"（多出来的值：{row[_EXTRA_KEY]!r}）"
                )

            day = self._parse_date(row, date_column, line_number)
            if day is None:
                skipped += 1
                continue
            grouped[day].append(row)

        if not grouped:
            raise ParseError(
                "没有解析出任何数据行"
                + (f"（有 {skipped} 行因为日期为空被跳过）" if skipped else "（只有表头）")
            )

        days = [RawRows(stat_date=day, rows=rows) for day, rows in sorted(grouped.items())]
        return ParseResult(days=days, skipped_rows=skipped)

    def _resolve_date_column(self, headers: Sequence[str]) -> str:
        if self._date_column is not None:
            if self._date_column not in headers:
                raise ParseError(
                    f"指定的日期列 {self._date_column!r} 不在表头里。表头是：{headers}"
                )
            return self._date_column

        for candidate in DATE_COLUMN_CANDIDATES:
            if candidate in headers:
                return candidate

        raise ParseError(
            f"找不到日期列。试过 {list(DATE_COLUMN_CANDIDATES)}，"
            f"而这份文件的表头是 {headers}。用 date_column 参数显式指定一个。"
        )

    def _parse_date(self, row: dict[str, Any], column: str, line_number: int) -> date | None:
        """解析某一行的日期。**返回 None 表示这行该跳过**（日期为空）。

        日期为空最常见的来源是导出文件末尾那行「Total」汇总。它不是错误，但也
        不该混进日数据里 —— 那一行的花费是整段时间的合计，落进任何一天都是错的。
        """
        value = row.get(column)
        raw = str(value).strip() if value is not None else ""
        if not raw:
            return None

        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

        raise ParseError(
            f"第 {line_number} 行的日期 {raw!r} 认不出来。支持的格式：{list(DATE_FORMATS)}"
        )
