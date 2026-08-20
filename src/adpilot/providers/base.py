"""数据源协议，以及它产出的东西。

这个模块**不 import 任何 adpilot 内部模块**，这是刻意的：适配器只认外部格式，
不认数据库、不认业务规则。分层契约里 `providers` 与 `db` 同层，靠的就是这一点。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


class ParseError(Exception):
    """外部数据解析失败。

    `message` 会经 `services/` 翻成 422 回给客户端，所以要写得能指导排查：**哪一
    行、哪个字段、期望什么**。「解析失败」这种消息等于没说 —— 导入的人拿着一个
    几千行的 CSV，没有行号根本无从下手。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class RawRows:
    """某一天的原始报表行，**未经任何解释**。

    `rows` 里的键就是外部系统给的键（CSV 的表头、API 的 JSON 字段名），一个字都
    没改过。字段映射是归一化那一步的事 —— 那是一次单向转换，映射规则改了或者
    发现 bug，拿这些行重跑就是了（`db/mongo.py` 的模块 docstring 讲了为什么值得
    为此多养一个数据库）。

    **不带 `account_id`**：那是数据库主键，provider 不该知道它存在。补主键、补
    `fetched_at`、写 Mongo，都是 `services/imports.py` 的事。
    """

    stat_date: date
    rows: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ParseResult:
    """一次解析的产物：按天分好组的行，加上被跳过的行数。

    **`skipped_rows` 不是可有可无的统计。** 平台后台导出的 CSV 末尾常带一行
    「Total」汇总，那行没有日期。直接报错会让每次导入都要人先手工删一行；静默
    丢掉又会让「导进去的行数对不上」变成一桩无头案。所以：跳过，但把数字报出来，
    由导入的人自己判断这个数合不合理。
    """

    days: Sequence[RawRows]
    skipped_rows: int


class ReportProvider(Protocol):
    """把外部格式变成原始行。

    **为什么签名是 `parse(content)`，而不是设计文档第四节里那个
    `fetch(account, day)`。** 那份骨架描述的是 API 拉取形态，而 MVP 落地的是文件
    导入，两者的输入根本不同：拉取型要拿 (账户, 日期区间) 去问平台；推送型拿到的
    是一个已经包含多天多对象的文件，日期是**从内容里读出来**的而不是入参。硬塞进
    同一个签名，结果是两边各带一半用不上的参数，而那种签名没人看得懂该传什么。

    平台 API 适配器接进来时的做法：在这个 Protocol 上加一个 `fetch` 方法，两种
    形态各有各的入口，共用 `RawRows` 这个产物和它下游的整条链路。**要统一的是
    产物，不是入口** —— 产物一致，归一化、重跑、审计才是同一套代码。
    """

    #: 落进 `raw_reports.provider` 的值，也是注册表的键。改它等于改历史快照的
    #: 来源标记，所以定了就不要动。
    name: str

    def parse(self, content: bytes) -> ParseResult: ...
