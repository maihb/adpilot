"""数据源协议，以及它产出的东西。

这个模块**不 import 任何 adpilot 内部模块**，这是刻意的：适配器只认外部格式，
不认数据库、不认业务规则。分层契约里 `providers` 与 `db` 同层，靠的就是这一点。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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


class FetchError(Exception):
    """从平台 API 拉数据失败。

    🔴 **`retryable` 是这个类存在的全部理由。** API 世界里的失败分两种，而把它们
    混成一种会让系统在最坏的情况下最安静：

    * **瞬时**（限流、502、连接超时）→ 退避重试，多半自己会好；
    * **确定性**（token 过期或被撤销、advertiser_id 不对、权限不足）→ 立刻停止
      重试并让人知道。重试一个已经失效的 token，五次退避之后任务安静地失败，而
      任务结果一天后就过期了 —— 第二天没人发现，第三天日报里的「花费 0」看起来
      还是很正常。

    **判定是平台知识，归各 provider 自己做**（错误码表长在平台那边）。上层只认
    这个布尔值：`tasks/` 据它决定重试还是直接进死信队列。

    ⚠️ **拿不准的时候填 `False`。** 不可重试的那条路会开告警、会有人看见；可
    重试的那条路会安静地自愈或安静地失败。误判成「不可重试」的代价是一条多余的
    告警，反过来的代价是数据静默停更好几天。
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """账户在某一刻的可用余额。

    **是时点量不是日量**，所以带的是 `captured_at` 而不是 `stat_date` ——
    同一天充值前后是两个完全不同的数（`models/balance.py` 讲了为什么这张表
    和日指标刻意不同构）。

    ⚠️ `captured_at` 是**平台口径的时刻**，拿不到就填拉取时刻。两者的差别在
    「刚充完值那几分钟」会显现，所以由 provider 决定填什么、并在落库时把来源
    写进 `note`，而不是让上层去猜。
    """

    available: Decimal
    currency: str
    captured_at: datetime


class FetchProvider(Protocol):
    """从平台 API 拉数据。**与 `ReportProvider` 是并列的两种形态，不是它的子类。**

    ## 为什么不合成一个 Protocol

    同步/异步这条线跨不过去：`parse` 必须留在 `asyncio.to_thread` 里（几千行 CSV
    在事件循环里解析会卡住**整个进程**的所有请求），而 `fetch` 是纯网络等待，丢
    进线程池只是白占一个线程。硬凑成一个方法的下场是每个调用点都要先判断「这个
    provider 是哪一种」—— 那正是 Protocol 想消掉的东西。

    **共用的是产物**（`ParseResult` / `RawRows`），于是落快照、归一化、重跑、审计
    全是同一套代码。这就是本模块开头那句「要统一的是产物，不是入口」。

    ## `level` 为什么是 `str` 而不是 `MetricLevel`

    本模块不 import 任何 adpilot 内部模块（见模块 docstring），而 `MetricLevel`
    在 `models` 里。所以这里收字符串（`"campaign"` / `"ad"`），由 `services/` 传
    `level.value` 进来；把它翻译成平台方言（TikTok 的 `AUCTION_CAMPAIGN`、维度
    `campaign_id`）是各 provider 自己的事 —— 那本来就是平台特有的知识。

    ## 拉回来的东西一个字段都不许改名

    和文件导入同一条规矩：`RawRows.rows` 里的键就是平台给的键。API 的响应是嵌套
    的（dimensions / metrics 两层），provider 可以把它**摊平成一层**，但不能改名
    —— 摊平只是去掉一层容器，改名会把当时的映射规则永久烧进快照，重跑也救不回来。
    """

    #: 落进 `raw_reports.provider` 的值。与文件型 provider 共用一个命名空间，
    #: 所以 TikTok 的 API provider 和将来可能出现的 TikTok CSV provider 必须
    #: 是两个名字 —— 它们的字段形状不一样，归一化要靠这个名字知道该怎么读。
    name: str

    async def fetch(
        self,
        *,
        external_id: str,
        level: str,
        since: date,
        until: date,
    ) -> ParseResult:
        """拉 `[since, until]`（**两端都含**）区间内的日指标。

        区间是闭的，因为平台的 `start_date` / `end_date` 参数就是闭区间 —— 在这里
        改成半开会让每个 provider 都得做一次 ±1 天的换算，而那种错不会报错，只会
        让每次拉取都少一天。
        """
        ...

    async def fetch_balance(self, *, external_id: str) -> AccountBalance:
        """拉当前可用余额。

        与 `fetch` 分开是因为它们的产物、频率和用途都不同：日指标是历史事实、
        可以补拉，余额是此刻的状态、补拉没有意义（过去某一刻的余额平台不提供）。
        """
        ...
