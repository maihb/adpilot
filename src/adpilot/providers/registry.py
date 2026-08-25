"""适配器注册表。

**新增一个数据源 = 实现 `ReportProvider` + 在这里登记一行**，上游一行不改。这是
[设计文档第四节](../../../docs/design/2026-08-19-mvp-design.md)点名要复用的那套
模式（作者在多模型图像生成里做过的 `ImageGenerator` 注册表）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from adpilot.providers import tiktok
from adpilot.providers.base import FetchProvider, ParseError, ReportProvider
from adpilot.providers.csv_file import CsvImportProvider
from adpilot.providers.fake_api import FakeFetchProvider
from adpilot.providers.tiktok import TikTokProvider

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


@dataclass(frozen=True, slots=True)
class FetchOptions:
    """造一个拉取型 provider 要的零件。

    `create` 那边的 docstring 预告过这一天：「接入平台 API provider 时的正确做法
    是收一个 options 映射、由各 provider 自己解释」。现在真有两个实现了，那个
    时机到了 —— 但**只在拉取这一侧**收。文件型那边仍然只有一个 `date_column`，
    给它也套一层 options 只会让调用点白白失去类型检查。
    """

    #: 一次授权换来的 token，覆盖一批广告主账户。假 provider 用不到它，但也
    #: 不给它默认值 —— 一个「不需要凭据」的拉取入口，正是最容易被误接进生产的
    #: 那种东西。
    access_token: SecretStr

    #: 空串表示用 provider 自己的默认地址。**沙盒就是靠它切换的**：审核通过之前
    #: 只有沙盒能用，而两者的差别应该只有这一个值。
    base_url: str = ""

    #: 额外请求的指标名。收入类指标最容易改名，而请求一个不存在的 metric 是
    #: 整个请求 400 —— 所以它们走配置，实测确认后填进环境变量即可生效，
    #: 理由见 `tiktok.py` 的模块 docstring。
    extra_metrics: tuple[str, ...] = ()


#: 名字 → 构造函数。与文件型那张表**刻意分开**：两种形态的构造参数根本不同，
#: 而合成一张表就意味着要有一个「两边都塞得下」的参数包，那种签名没人看得懂
#: 该传什么（`base.py` 里 `FetchProvider` 的 docstring 讲的是同一件事）。
_FETCH_FACTORIES: dict[str, Callable[[FetchOptions], FetchProvider]] = {
    TikTokProvider.name: lambda options: TikTokProvider(
        access_token=options.access_token,
        base_url=options.base_url or tiktok.DEFAULT_BASE_URL,
        extra_metrics=options.extra_metrics,
    ),
    FakeFetchProvider.name: lambda _options: FakeFetchProvider(),
}

#: 只在非生产环境可用的 provider。假数据进了生产库不会报错，只会让某个客户的
#: 看板上多出几行「demo-」开头的花费 —— 而它和真实数据长得一样，会被一起算进
#: 汇总。同 `seed.py` 在 prod 下拒绝执行。
_DEV_ONLY: frozenset[str] = frozenset({FakeFetchProvider.name})


def available_fetch(*, allow_fake: bool = False) -> list[str]:
    """已注册的拉取型 provider，供后台把可选值列进下拉框。"""
    return sorted(name for name in _FETCH_FACTORIES if allow_fake or name not in _DEV_ONLY)


def create_fetch(
    name: str,
    options: FetchOptions,
    *,
    allow_fake: bool = False,
) -> FetchProvider:
    """按名字造一个拉取型 provider。

    `allow_fake` 由调用方（`services/`）按 `ENVIRONMENT` 决定 —— 这一层不认识
    应用配置，收一个布尔值比收一个 `Settings` 干净，理由同各 provider 的构造
    参数「收零件不收配置」。
    """
    if name in _DEV_ONLY and not allow_fake:
        raise ParseError(f"{name!r} 只能在非生产环境使用")

    factory = _FETCH_FACTORIES.get(name)
    if factory is None:
        raise ParseError(
            f"没有这个数据源：{name!r}。可选：{available_fetch(allow_fake=allow_fake)}"
        )
    return factory(options)
