"""适配器注册表。

**新增一个数据源 = 实现 `ReportProvider` + 在这里登记一行**，上游一行不改。这是
[设计文档第四节](../../../docs/design/2026-08-19-mvp-design.md)点名要复用的那套
模式（作者在多模型图像生成里做过的 `ImageGenerator` 注册表）。
"""

from __future__ import annotations

from collections.abc import Callable

from adpilot.providers.base import ParseError, ReportProvider
from adpilot.providers.csv_file import CsvImportProvider

# 名字 → 构造函数。名字会原样落进 `raw_reports.provider`，是历史快照的来源标记，
# 所以定了就不要改 —— 改了之后旧文档指向一个不存在的 provider，重跑归一化时
# 找不到该用哪个映射规则。
_FACTORIES: dict[str, Callable[[str | None], ReportProvider]] = {
    CsvImportProvider.name: CsvImportProvider,
}


def available() -> list[str]:
    """已注册的 provider 名字，供接口把可选值列进 OpenAPI。"""
    return sorted(_FACTORIES)


def create(name: str, *, date_column: str | None = None) -> ReportProvider:
    """按名字造一个 provider。

    `date_column` 是文件导入特有的参数。**接入平台 API provider 时这个签名要改**
    —— 那时的正确做法是收一个 options 映射、由各 provider 自己解释。现在不那么
    做，是因为只有一个 provider 时，一个 `options: dict` 只会让调用点失去类型
    检查，换来一个还用不上的灵活性。
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ParseError(f"没有这个数据源：{name!r}。可选：{available()}")
    return factory(date_column)
