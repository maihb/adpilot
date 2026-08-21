"""店铺后台导出的库存表。

**不是 `ReportProvider`，也不进那个注册表。** 两者形状不同、下游也不同：

| | `CsvImportProvider` | 这个 |
|---|---|---|
| 产物 | 按天分组的**原始行**，一个字段都不映射 | 已认出字段的库存条目 |
| 去向 | Mongo `raw_reports`，再归一化 | 直接进 PG |
| 为什么 | 平台字段会漂移，原始事实要留痕 | 表的形状**由我们自己定**，没有平台在改它 |

为一份自定义模板再走一遍「原始快照 → 归一化」，是把机制当仪式：多两个失败点、多
一次异步等待，换来的审计价值是零。完整论证见[库存断货设计][d]第二节。

⚠️ 接了店铺 API 之后这条要重看 —— 那时进来的是平台原始 JSON，字段会漂移，那份
响应该进 Mongo，和广告报表一样。

[d]: ../../../docs/design/2026-08-21-stock-alerts.md
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from adpilot.providers.base import ParseError

# 各家店铺后台对这几列的叫法。探测不到必填列就报错，**不猜** —— 同
# `csv_file.py` 那条：猜错的后果是整份库存挂到错的字段上，而页面上看起来完全正常。
SKU_COLUMNS = ("sku", "SKU", "商品编码", "货号", "款号", "variant_sku", "Variant SKU")
NAME_COLUMNS = ("name", "商品名称", "商品名", "标题", "title", "Title", "product_name")
QTY_COLUMNS = (
    "stock",
    "qty",
    "quantity",
    "库存",
    "可售库存",
    "剩余库存",
    "inventory",
    "Inventory",
    "Variant Inventory Qty",
)
#: 日均销量这一列**是可选的**。没有它时日均由 `rules/stock.py` 从快照序列自己推。
DAILY_SALES_COLUMNS = ("daily_sales", "日均销量", "日均销售", "avg_daily_sales", "日销")

#: 行比表头长时多出来的值会落到这个键上。理由同 `csv_file.py`：默认是 `None`，
#: 而一个 `None` 键会让下游在很远的地方以看不懂的方式失败。
_EXTRA_KEY = "_extra_columns"

#: 千分位分隔符和常见的单位后缀。店铺后台导出「1,240 件」是常事，而
#: `Decimal("1,240")` 直接抛异常，报错里只说「不是数字」。
_STRIPPED = str.maketrans({",": None, "，": None, " ": None})
_UNIT_SUFFIXES = ("件", "个", "pcs", "PCS")


@dataclass(frozen=True, slots=True)
class StockRow:
    """文件里的一行：一个 SKU 此刻的库存。

    **不带 `client_id`、不带 `captured_at`** —— 前者是数据库主键、后者是导入时的
    元数据，都由 `services/product.py` 补上。这一层只认文件内容（同 `RawRows`
    那条「provider 不该知道主键存在」）。
    """

    sku: str
    name: str | None
    qty: Decimal

    #: 文件里带了日均销量列就有值，没带就是 `None`。**两者不能混为一谈**：
    #: `None` 是「这份导出没有这一列」，`0` 是「近期一件没卖」。
    daily_sales: Decimal | None


@dataclass(frozen=True, slots=True)
class StockParseResult:
    rows: Sequence[StockRow]

    #: 被跳过的行数。SKU 为空的行（导出末尾的合计行、分隔行）不算错误，但数字要
    #: 报出来 —— 理由同 `ParseResult.skipped_rows`：静默丢掉会让「导进去的条数
    #: 对不上」变成一桩无头案。
    skipped_rows: int


class StockCsvParser:
    """把一份库存导出解析成条目。"""

    name = "stock_csv"

    def parse(self, content: bytes) -> StockParseResult:
        # utf-8-sig 的理由同 `csv_file.py`：Excel 另存的 CSV 带 BOM，用普通 utf-8
        # 解码会让第一个表头变成 "﻿sku"，于是列探测失败 —— 而那个 BOM 在报错信息里
        # 是**看不见的**。
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("文件不是 UTF-8 编码，导出时请选 UTF-8（或先转码）") from exc

        reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA_KEY)
        if reader.fieldnames is None:
            raise ParseError("这个 CSV 是空的，连表头都没有")

        headers = list(reader.fieldnames)
        sku_column = _require_column(headers, SKU_COLUMNS, "商品编码")
        qty_column = _require_column(headers, QTY_COLUMNS, "库存")
        name_column = _find_column(headers, NAME_COLUMNS)
        sales_column = _find_column(headers, DAILY_SALES_COLUMNS)

        rows: list[StockRow] = []
        seen: dict[str, int] = {}
        skipped = 0
        for line_number, raw_row in enumerate(reader, start=2):  # 第 1 行是表头
            row = dict(raw_row)
            if _EXTRA_KEY in row:
                raise ParseError(
                    f"第 {line_number} 行的列数比表头多，文件可能被改坏了"
                    f"（多出来的值：{row[_EXTRA_KEY]!r}）"
                )

            sku = _text(row.get(sku_column))
            if not sku:
                # 导出末尾的「合计」行、或者分类之间的空行。不是错误。
                skipped += 1
                continue

            # 同一份文件里同一个 SKU 出现两次，**当场报错不去猜**：可能是导出时
            # 按仓库分了行（该相加）、也可能是复制粘贴出的重复（该去重），两种
            # 处理方式得出的库存差着一倍，而两者在文件里长得一模一样。
            if sku in seen:
                raise ParseError(
                    f"第 {line_number} 行的商品编码 {sku!r} 在第 {seen[sku]} 行已经出现过。"
                    "同一份文件里一个编码只能有一行 —— 如果是按仓库分开导的，请先合并再上传"
                )
            seen[sku] = line_number

            rows.append(
                StockRow(
                    sku=sku,
                    name=_text(row.get(name_column)) if name_column else None,
                    qty=_required_decimal(row.get(qty_column), line_number, qty_column),
                    daily_sales=(
                        _optional_decimal(row.get(sales_column), line_number, sales_column)
                        if sales_column
                        else None
                    ),
                )
            )

        if not rows:
            raise ParseError(
                "没有解析出任何商品行"
                + (f"（有 {skipped} 行因为商品编码为空被跳过）" if skipped else "（只有表头）")
            )

        return StockParseResult(rows=rows, skipped_rows=skipped)


def _find_column(headers: Sequence[str], candidates: Sequence[str]) -> str | None:
    """按候选名找一列，找不到返回 `None`。大小写不敏感，两端空白不算数。"""
    normalized = {header.strip().lower(): header for header in headers}
    for candidate in candidates:
        found = normalized.get(candidate.strip().lower())
        if found is not None:
            return found
    return None


def _require_column(headers: Sequence[str], candidates: Sequence[str], label: str) -> str:
    column = _find_column(headers, candidates)
    if column is None:
        raise ParseError(
            f"找不到{label}那一列。试过 {list(candidates)}，而这份文件的表头是 {list(headers)}"
        )
    return column


def _text(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _required_decimal(value: object, line_number: int, column: str) -> Decimal:
    """必填的那一格（库存）。空着就报错 —— 空库存和 0 库存差得远。"""
    parsed = _optional_decimal(value, line_number, column)
    if parsed is None:
        raise ParseError(f"第 {line_number} 行的 {column!r} 是空的，库存不能为空")
    return parsed


def _optional_decimal(value: object, line_number: int, column: str) -> Decimal | None:
    """把一格解析成 `Decimal`，空着返回 `None`。

    **不经 `float`**（conventions.md 那条「沾数的地方不用浮点」）。这里的数字是
    件数不是金额，但同一条规矩适用：`float("0.1") * 3` 那类误差会一路漂进可撑
    天数，而没有任何东西会报错。

    🔴 **空格子返回 `None` 而不是 0。** 有「日均销量」这一列、但某个款没填，意思
    是「这个款的销量没导出来」，该交给规则从库存变化去推；读成 0 则是「近期一件
    没卖」，于是这个款**永远不会告警** —— 一个安静的漏报。
    """
    raw = _text(value)
    if raw is None:
        return None

    cleaned = raw.translate(_STRIPPED)
    for suffix in _UNIT_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    try:
        parsed = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"第 {line_number} 行的 {column!r} 认不出是数字：{raw!r}") from exc

    if parsed < 0:
        # 负库存在有些后台里表示「超卖」。它是真实存在的状态，但对这条规则来说
        # 「已经卖光了」和「超卖了 3 件」要采取的行动完全一样，而放它进来会让
        # 可撑天数变成负数。夹到 0，语义不丢。
        return Decimal(0)
    return parsed
