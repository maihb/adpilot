"""脱敏示例数据：让全套环境起来之后立刻有东西可看。

`docker compose up` 跑完是一个**空库** —— 接口全通、健康检查全绿、一条数据也没有。
陌生人 clone 下来看到的就是这个，而设计文档给 D14 定的验收标准是「五分钟内跑起来」。
跑起来不等于看得见东西，这个模块补的是后半句。

命令：`make seed`（展开是 `uv run python -m adpilot.seed`）。

## 只添不改

「不存在则创建，存在则**完全不动**」——重复跑不会覆盖任何已有的行。

不做 upsert 覆盖的理由不是幂等（`DO UPDATE` 一样幂等），而是：有人跑完 seed 之后
去改示例账户的时区、拿它试跨时区的日切，再跑一次 seed 就把他的改动静默还原了，
而他只会觉得「我明明改过」。**配置赢在无声处**是最难查的一类问题。

## 生产环境直接拒绝

`ENVIRONMENT=prod` 时拒绝执行，且**不提供 `--force`**。往生产库灌四个假客户没有
任何正当理由，而留一个开关就等于没有这道护栏。真要在类生产环境演示，改
`ENVIRONMENT` —— 那是一个需要动手、且能在 `.env` 里看见的动作。

## 四个账户覆盖四种规则结局

示例数据不能只有 happy path，否则规则引擎有没有接对根本看不出来。跑完 `make seed`
再跑一次巡检（`POST /api/alerts/sweep`，或等 beat 每小时那一次），应当**恰好**得到
两条告警：

| 账户 | 规则结局 |
|---|---|
| 家居优选 / Meta | 一切正常：余额够撑约 15 天，昨天指标平稳 |
| 家居优选 / TikTok | 🔔 `balance_low` —— 余额只够撑约 2 天，低于 `rules/balance.py` 的阈值 |
| 美妆日记 / Meta | 🔔 `metric_anomaly` —— 昨天花一样的钱、转化少了一半，CPA 翻倍 |
| 户外装备 / TikTok | **不告警**：暂停投放，日均消耗为 0 → 可撑天数**无定义** |

最后一行是这批数据里最有价值的一条：「没有消耗就不会归零」这条边界最容易被写成
「0 天，立刻告警」，而那样的告警会让人对整个告警列表脱敏。有一份数据天天覆盖着它，
比一句注释管用。

## 数字怎么来的

账户级的基准值写在下面的 `_PLAN` 里，每天按 ±`_JITTER`% 波动，其余指标由它推导
（`impressions` 由 CPM 推、`conversions` 由 CPA 推、`revenue` 由客单价推），所以
CPM / CPC / CTR / CPA / ROAS 这些派生指标现算出来都在合理区间，不会出现「ROAS 是
0.02」这种一眼假的数。

**全程 `Decimal`，不经过 `float` 中转**（conventions.md「金额」）。随机波动用
`randint` 而不是 `uniform`，正是因为后者返回 float。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from random import Random
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings, get_settings
from adpilot.db.postgres import create_engine, create_session_factory, transaction
from adpilot.logging import configure_logging
from adpilot.models.ad_account import AdAccount, Platform
from adpilot.models.balance import Balance
from adpilot.models.client import Client
from adpilot.models.daily_metric import DailyMetric, MetricLevel
from adpilot.rules import balance as balance_rules
from adpilot.services import ad_account as ad_account_service
from adpilot.services import balance as balance_service
from adpilot.services import client as client_service
from adpilot.services import invite as invite_service

log = structlog.get_logger(__name__)

#: 造多少天历史。下限由规则定：指标异动要 8 天（昨天 + 上周同日），余额日均回看
#: 7 天。取 28 天是为了按天查询的接口有一段像样的区间可翻，同时一次 seed 只写几
#: 百行、不至于让人等。
_HISTORY_DAYS = 28

#: 日指标的随机波动幅度，百分比。
#:
#: 🔴 **这个上限是算出来的，不是拍的。** 周同比的最坏情况是 `(1+j) / (1-j)`：取 10
#: 时约 +22%，离 `rules/anomaly.py` 的 40% 阈值还有距离，所以正常账户不会因为随机
#: 波动误触发异动告警。调大它之前先回去看那个阈值 —— 到 18 就开始偶发误报了，而
#: 症状是「seed 出来的告警数量时多时少」，很难联想到这一行。
_JITTER = 10

#: 覆盖人数占展示次数的比例。不参与任何规则判定，只是让 `reach` 这一列不为空 ——
#: 它跨天不可加（见 `models/daily_metric.py`），这里也就不必讲究。
_REACH_RATIO = Decimal("0.68")

#: 与 `numeric(20,4)` 对齐。显式量化而不是交给 PostgreSQL 隐式舍入，是为了让
#: 「算出来的」和「存进去的」是同一个数（conventions.md「金额」最后一条）。
_MONEY = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class _Campaign:
    """一个广告系列。"""

    external_id: str
    name: str
    #: 占账户总量的比例。同一账户下所有系列**必须加起来等于 1**，
    #: `tests/test_seed.py` 盯着这件事。
    share: Decimal


@dataclass(frozen=True, slots=True)
class _Account:
    """一个广告账户的画像。基准值 + 波动就是这个账户的全部数据。"""

    external_id: str
    name: str
    platform: Platform
    currency: str
    timezone: str
    campaigns: tuple[_Campaign, ...]

    #: 账户级基准日消耗，账户币种。
    base_spend: Decimal
    #: 基准单次转化成本。`conversions` 由 `spend / cpa` 推出来。
    base_cpa: Decimal
    #: 客单价，用来推 `revenue`（于是 ROAS 也就有了）。
    aov: Decimal
    #: 千次展示成本与点击率，用来推 `impressions` / `clicks`。
    cpm: Decimal
    ctr: Decimal

    #: 余额够撑几天 —— **这是这个账户会不会触发余额告警的开关**。
    #:
    #: 写成天数而不是直接写金额，是为了让意图一眼可读：2 必然低于
    #: `rules/balance.py` 的 3 天阈值，15 必然高于。金额由它乘以实际日均算出来，
    #: 所以阈值那天改了，这里的语义仍然成立。`None` 表示不录余额快照。
    balance_days: Decimal | None

    #: 暂停投放：整段历史 `spend` 全 0。
    #:
    #: ⚠️ 暂停账户的 `balance_days` 只用来定金额，**不代表实际可撑天数** —— 日均
    #: 消耗为 0 时这个除法根本没有定义，那正是它要演示的东西。
    paused: bool = False

    #: 昨天的 CPA 乘数。填了就在昨天那天把 CPA 抬上去触发指标异动。
    #:
    #: **只抬 CPA、不动 `spend`**：花一样的钱、转化少了一半，这既是真实的疲劳
    #: 场景，也让「花费」那条异动规则保持不触发 —— 于是这个账户恰好产出一条告警，
    #: 而不是两条。
    cpa_spike: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _Client:
    """一个客户及其账户。"""

    name: str
    note: str
    accounts: tuple[_Account, ...]


# 🔴 名字、账户 ID、系列 ID 全部是**编造的**，且带 `示例` / `demo-` 前缀 ——
# 真实客户名与广告账户 ID 不进仓库（CLAUDE.md 第 1 条硬规矩，设计文档第八节）。
# 往这张表里加数据时照着这个前缀写。
_PLAN: tuple[_Client, ...] = (
    _Client(
        name="示例｜家居优选",
        note="演示数据，非真实客户。两个账户同属美西时区，一个正常、一个余额告急。",
        accounts=(
            _Account(
                external_id="demo-meta-0001",
                name="家居优选 - Meta 主账户",
                platform=Platform.META,
                currency="USD",
                timezone="America/Los_Angeles",
                base_spend=Decimal(420),
                base_cpa=Decimal(26),
                aov=Decimal(88),
                cpm=Decimal("9.5"),
                ctr=Decimal("0.014"),
                balance_days=Decimal(15),
                campaigns=(
                    _Campaign("demo-cmp-0001", "夏季新品 - 宽定向", Decimal("0.50")),
                    _Campaign("demo-cmp-0002", "再营销 - 加购未购", Decimal("0.30")),
                    _Campaign("demo-cmp-0003", "爆款单品 - 相似人群", Decimal("0.20")),
                ),
            ),
            _Account(
                external_id="demo-tiktok-0001",
                name="家居优选 - TikTok 预充账户",
                platform=Platform.TIKTOK,
                currency="USD",
                timezone="America/Los_Angeles",
                base_spend=Decimal(180),
                base_cpa=Decimal(31),
                aov=Decimal(76),
                cpm=Decimal("6.2"),
                ctr=Decimal("0.011"),
                # 2 天 < 阈值 3 天 → 触发 balance_low。
                balance_days=Decimal(2),
                campaigns=(
                    _Campaign("demo-cmp-0004", "短视频引流 - 泛人群", Decimal("0.60")),
                    _Campaign("demo-cmp-0005", "直播间引流", Decimal("0.40")),
                ),
            ),
        ),
    ),
    _Client(
        name="示例｜美妆日记",
        note="演示数据，非真实客户。人民币结算、东八区，昨天 CPA 翻倍。",
        accounts=(
            _Account(
                external_id="demo-meta-0002",
                name="美妆日记 - Meta 主账户",
                platform=Platform.META,
                currency="CNY",
                timezone="Asia/Shanghai",
                base_spend=Decimal(2600),
                base_cpa=Decimal(145),
                aov=Decimal(420),
                cpm=Decimal(42),
                ctr=Decimal("0.016"),
                balance_days=Decimal(30),
                # 昨天 CPA ×2.2 → 周同比涨 80%~169%，稳定越过 40% 阈值。
                cpa_spike=Decimal("2.2"),
                campaigns=(
                    _Campaign("demo-cmp-0006", "护肤套装 - 大促预热", Decimal("0.45")),
                    _Campaign("demo-cmp-0007", "口红新色 - 达人相似", Decimal("0.35")),
                    _Campaign("demo-cmp-0008", "会员复购 - 老客再营销", Decimal("0.20")),
                ),
            ),
        ),
    ),
    _Client(
        name="示例｜户外装备",
        note="演示数据，非真实客户。账户已暂停投放 —— 用来验证「日均为 0 不告警」。",
        accounts=(
            _Account(
                external_id="demo-tiktok-0002",
                name="户外装备 - TikTok 德语区",
                platform=Platform.TIKTOK,
                currency="EUR",
                timezone="Europe/Berlin",
                # 暂停前的投放规模，只用来定余额金额，不会真的产生消耗。
                base_spend=Decimal(95),
                base_cpa=Decimal(38),
                aov=Decimal(140),
                cpm=Decimal("5.4"),
                ctr=Decimal("0.009"),
                balance_days=Decimal(12),
                paused=True,
                campaigns=(
                    _Campaign("demo-cmp-0009", "登山装备 - 德语区", Decimal("0.65")),
                    _Campaign("demo-cmp-0010", "露营周边 - 再营销", Decimal("0.35")),
                ),
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """跑完做了什么。

    **跳过的数量也要报出来**：「跑了 seed 却什么都没看见」和「跑了 seed 发现本来
    就有」是两回事，只报「成功」的话，两者长得一模一样。
    """

    clients_created: int = 0
    clients_existing: int = 0
    accounts_created: int = 0
    accounts_existing: int = 0
    metrics_inserted: int = 0
    metrics_existing: int = 0
    balances_recorded: int = 0
    balances_existing: int = 0


@dataclass(frozen=True, slots=True)
class _Figures:
    """某个账户某一天的账户级数字。"""

    spend: Decimal
    impressions: int
    clicks: int
    conversions: Decimal
    revenue: Decimal
    reach: int


def _yesterday(timezone: str) -> date:
    """账户时区下的昨天。

    🔴 **必须与 `services/alert.py` 的 `_yesterday()` 同口径。** 那边按账户时区取
    昨天去比周同比，这边按服务器时区造数据的话，两者在日切点附近会错开一天 ——
    症状是「seed 明明造了异动，巡检就是不报」，而且只在一天里的某几个小时复现。
    """
    return datetime.now(ZoneInfo(timezone)).date() - timedelta(days=1)


def _jitter(base: Decimal, rng: Random) -> Decimal:
    """给基准值加上 ±`_JITTER`% 的波动。

    用 `randint` 而不是 `uniform`：后者返回 `float`，而金额一律 `Decimal`、
    **不许经过 float 中转**（conventions.md「金额」）。
    """
    return base * (Decimal(100 + rng.randint(-_JITTER, _JITTER)) / 100)


def _round_int(value: Decimal) -> int:
    """四舍五入到整数。

    不用 `int()`：它是截断，用在这里会让每一行的展示与点击都系统性偏低。
    """
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _figures(plan: _Account, day: date, yesterday: date) -> _Figures:
    """算某账户某天的账户级数字。

    **随机种子按 `(账户, 日期)` 派生，不是顺序消费一个全局 `Random`。** 于是同一天
    重复跑得到完全相同的数字，往前补历史时早先那些天也不会跟着变 —— 顺序相关的
    随机数在这两件事上都会出岔子，而「昨天的数据昨天跑和今天跑不一样」会让任何
    对着示例数据写的测试变成薛定谔的。
    """
    if plan.paused:
        # 暂停投放：有指标行、`spend` 是 0。**这和「没有数据」不是一回事** ——
        # `rules/balance.py` 的日均分母正是靠这个区分（那个 docstring 有完整解释）。
        return _Figures(Decimal(0), 0, 0, Decimal(0), Decimal(0), 0)

    rng = Random(f"{plan.external_id}:{day.isoformat()}")

    spend = _jitter(plan.base_spend, rng).quantize(_MONEY)
    cpa = _jitter(plan.base_cpa, rng)
    if plan.cpa_spike is not None and day == yesterday:
        cpa *= plan.cpa_spike

    impressions = _round_int(spend / plan.cpm * 1000)
    return _Figures(
        spend=spend,
        impressions=impressions,
        clicks=_round_int(Decimal(impressions) * plan.ctr),
        conversions=(spend / cpa).quantize(_MONEY),
        revenue=(spend / cpa * plan.aov).quantize(_MONEY),
        reach=_round_int(Decimal(impressions) * _REACH_RATIO),
    )


def _split_money(total: Decimal, shares: Sequence[Decimal]) -> list[Decimal]:
    """按比例拆金额，**最后一份吃掉余数**。

    逐份四舍五入再相加，合计会跟账户级差几分钱。而「账户级和系列级加起来对不上」
    正是这套数据最先被质疑的地方 —— 在信任这件事上，差一分和差一万是一回事。
    """
    parts = [(total * share).quantize(_MONEY) for share in shares[:-1]]
    parts.append(total - sum(parts))
    return parts


def _split_int(total: int, shares: Sequence[Decimal]) -> list[int]:
    """按比例拆整数，最后一份吃余数。理由同 `_split_money`。"""
    parts = [_round_int(Decimal(total) * share) for share in shares[:-1]]
    parts.append(total - sum(parts))
    return parts


def _metric_rows(plan: _Account, day: date, yesterday: date) -> list[dict[str, Any]]:
    """一个账户某一天的全部指标行：账户级一行 + 每个系列一行。

    账户级是**权威的那一行** —— `services/daily_metric.py` 的汇总按 `LEVEL_PRIORITY`
    只取一个层级，账户级优先。系列级只用于展示，所以它们必须加起来正好等于账户级，
    否则翻明细的人会先怀疑数据、再怀疑整个系统。
    """
    figures = _figures(plan, day, yesterday)
    shares = [campaign.share for campaign in plan.campaigns]

    common = {"account_id": plan.external_id, "currency": plan.currency, "stat_date": day}
    rows: list[dict[str, Any]] = [
        {
            **common,
            "level": MetricLevel.ACCOUNT,
            # level=ACCOUNT 时 object_id 填账户自己的 external_id，让唯一键在四个
            # 层级上是同一个形状（`models/daily_metric.py` 的注释）。
            "object_id": plan.external_id,
            "object_name": plan.name,
            "spend": figures.spend,
            "impressions": figures.impressions,
            "clicks": figures.clicks,
            "conversions": figures.conversions,
            "revenue": figures.revenue,
            "reach": figures.reach,
        }
    ]

    spends = _split_money(figures.spend, shares)
    conversions = _split_money(figures.conversions, shares)
    revenues = _split_money(figures.revenue, shares)
    impressions = _split_int(figures.impressions, shares)
    clicks = _split_int(figures.clicks, shares)
    reaches = _split_int(figures.reach, shares)

    for index, campaign in enumerate(plan.campaigns):
        rows.append(
            {
                **common,
                "level": MetricLevel.CAMPAIGN,
                "object_id": campaign.external_id,
                "object_name": campaign.name,
                "spend": spends[index],
                "impressions": impressions[index],
                "clicks": clicks[index],
                "conversions": conversions[index],
                "revenue": revenues[index],
                "reach": reaches[index],
            }
        )
    return rows


async def _ensure_client(session: AsyncSession, plan: _Client) -> tuple[Client, bool]:
    """建客户；已存在就原样返回。返回 `(客户, 是不是新建的)`。

    「存在就完全不动」—— 不覆盖 `note`，也不把停用的客户改回启用。
    """
    existing = await session.scalar(select(Client).where(Client.name == plan.name))
    if existing is not None:
        return existing, False
    created = await client_service.create(session, name=plan.name, note=plan.note)
    return created, True


async def _ensure_account(
    session: AsyncSession, plan: _Account, client_id: int
) -> tuple[AdAccount, bool]:
    """建账户；已存在就原样返回。按 `(平台, 账户 ID)` 认，那是这张表的唯一键。"""
    existing = await session.scalar(
        select(AdAccount).where(
            AdAccount.platform == plan.platform,
            AdAccount.external_id == plan.external_id,
        )
    )
    if existing is not None:
        return existing, False
    created = await ad_account_service.create(
        session,
        client_id=client_id,
        platform=plan.platform,
        external_id=plan.external_id,
        name=plan.name,
        currency=plan.currency,
        timezone=plan.timezone,
    )
    return created, True


async def _insert_metrics(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """批量插入日指标，已存在的**跳过**。返回真正插进去的行数。

    `DO NOTHING` 而不是 `normalize.py` 那个 `DO UPDATE`：那边是重导要覆盖（平台会
    回填历史），这边是 seed，只添不改。
    """
    if not rows:
        return 0
    statement = insert(DailyMetric).values(rows)
    # 用 RETURNING 数行数而不是读 `rowcount`：`DO NOTHING` 只把真正插进去的那些
    # 返回回来，正好是要的数字，而 `Result.rowcount` 在类型上够不着（要 cast 成
    # CursorResult 才行），为一个几百行的语句不值得。
    result = await session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["account_id", "level", "object_id", "stat_date"],
        ).returning(DailyMetric.id)
    )
    return len(result.scalars().all())


async def _seed_account(
    session: AsyncSession, plan: _Account, account: AdAccount
) -> tuple[int, int, bool]:
    """给一个账户造历史指标和余额快照。

    返回 `(插入的指标行数, 跳过的指标行数, 是否新录了余额)`。
    """
    yesterday = _yesterday(plan.timezone)
    days = [yesterday - timedelta(days=offset) for offset in range(_HISTORY_DAYS)]

    rows: list[dict[str, Any]] = []
    for day in days:
        rows.extend(_metric_rows(plan, day, yesterday))
    # 账户 ID 在计划里是平台侧的字符串，落库要换成本地主键。
    for row in rows:
        row["account_id"] = account.id

    inserted = await _insert_metrics(session, rows)

    amount = _balance_amount(plan, yesterday)
    if amount is None:
        return inserted, len(rows) - inserted, False

    balance_recorded = await _ensure_balance(session, plan, account, yesterday, amount)
    return inserted, len(rows) - inserted, balance_recorded


def _average_daily_spend(plan: _Account, yesterday: date) -> Decimal:
    """按规则的回看窗口算日均消耗。

    窗口长度跟着 `rules/balance.py` 的 `LOOKBACK_DAYS` 走、不写死 7 —— 那个参数
    标着「待定」，改了之后这里算出来的余额才不会跟规则脱节。
    """
    window = [yesterday - timedelta(days=offset) for offset in range(balance_rules.LOOKBACK_DAYS)]
    total = sum((_figures(plan, day, yesterday).spend for day in window), Decimal(0))
    return balance_rules.average_daily_spend(total, len(window))


def _balance_amount(plan: _Account, yesterday: date) -> Decimal | None:
    """要录的余额金额；`None` 表示这个账户不录余额快照。

    金额 = **实际生成的日均消耗** × `balance_days`，不是拿基准值乘：随机波动会让
    日均偏离基准几个百分点，而「够撑 2 天」那个账户的整个存在意义就是稳稳落在
    `ALERT_THRESHOLD_DAYS` 下方。

    ⚠️ **暂停账户是例外，退回基准值。** 它的实际日均是 0，乘出来余额也是 0 ——
    而「暂停投放」和「账户没钱了」是两回事，显示成 0 会让人读成后者，恰好毁掉这个
    账户想演示的那条边界。退回基准值之后它表达的是「按暂停前的规模还能撑多久」。
    """
    if plan.balance_days is None:
        return None
    daily = plan.base_spend if plan.paused else _average_daily_spend(plan, yesterday)
    return (daily * plan.balance_days).quantize(_MONEY)


async def _ensure_balance(
    session: AsyncSession,
    plan: _Account,
    account: AdAccount,
    yesterday: date,
    amount: Decimal,
) -> bool:
    """录一条余额快照；同一时刻已经有了就跳过。返回是不是新录的。

    `captured_at` 取账户时区下昨天的 20:00 —— 一个确定的、必定在过去的时刻。
    取「现在」的话每次跑都是新的一行，那就不幂等了；取今天某个钟点则可能落在未来。
    """
    captured_at = datetime.combine(yesterday, time(20, 0), tzinfo=ZoneInfo(plan.timezone))

    existing = await session.scalar(
        select(Balance).where(
            Balance.account_id == account.id,
            Balance.captured_at == captured_at,
        )
    )
    if existing is not None:
        return False

    await balance_service.record(
        session,
        account_id=account.id,
        available=amount,
        captured_at=captured_at,
        note="示例数据",
    )
    return True


async def seed(session: AsyncSession) -> SeedSummary:
    """把 `_PLAN` 灌进库里。只添不改，重复跑安全。"""
    summary = SeedSummary()

    for client_plan in _PLAN:
        client, client_created = await _ensure_client(session, client_plan)
        summary = _bump(
            summary,
            clients_created=int(client_created),
            clients_existing=int(not client_created),
        )

        for account_plan in client_plan.accounts:
            account, account_created = await _ensure_account(session, account_plan, client.id)
            inserted, skipped, balance_recorded = await _seed_account(
                session, account_plan, account
            )
            summary = _bump(
                summary,
                accounts_created=int(account_created),
                accounts_existing=int(not account_created),
                metrics_inserted=inserted,
                metrics_existing=skipped,
                balances_recorded=int(balance_recorded),
                balances_existing=int(
                    account_plan.balance_days is not None and not balance_recorded
                ),
            )

    log.info(
        "seed_done",
        clients_created=summary.clients_created,
        accounts_created=summary.accounts_created,
        metrics_inserted=summary.metrics_inserted,
        balances_recorded=summary.balances_recorded,
    )
    return summary


def _bump(summary: SeedSummary, **deltas: int) -> SeedSummary:
    """累加计数。`SeedSummary` 是 frozen 的，所以造一个新的。"""
    return SeedSummary(
        **{
            field: getattr(summary, field) + deltas.get(field, 0)
            for field in SeedSummary.__dataclass_fields__
        }
    )


async def issue_demo_invites(session: AsyncSession) -> list[tuple[str, str]]:
    """给**每个**示例客户各发一个邀请码，返回 [(客户名, **明文码**)]。

    **每次跑 seed 都新发一批**，旧的仍然有效。这看起来违反了本模块「只添不改」
    的调子，其实正合它：新发是「添」，而明文码存不下来（库里是哈希），不重新发
    的话第二次跑 seed 的人就再也拿不到一个能用的码了。

    🔴 **码是随机生成的，不是写死在源码里的常量。** 写死一个「demo 码」等于往
    公开仓库里放一把人人皆知的钥匙 —— 哪怕它只对示例数据有效，也会有人把 seed
    跑在一个已经有真实客户的库上。有测试盯着这件事（`tests/test_seed.py`）。

    **为什么每个客户都发**（原先只发第一个）：四个示例账户各演示一种规则结局，
    而客户端一张票只能看一个客户。只发第一个客户的码，那条最容易写错的边界 ——
    「暂停投放 → 日均为 0 → 可撑天数**无定义**，不是 0 天」—— 在界面上就永远
    看不到，因为那个账户在最后一个客户名下。
    """
    issued: list[tuple[str, str]] = []
    for plan in _PLAN:
        client = await session.scalar(select(Client).where(Client.name == plan.name))
        if client is None:  # pragma: no cover - seed 刚跑完，这些客户一定都在
            raise RuntimeError(f"示例客户不存在：{plan.name}")

        _, code = await invite_service.create(session, client_id=client.id)
        issued.append((client.name, code))
    return issued


async def _run(settings: Settings) -> tuple[SeedSummary, list[tuple[str, str]]]:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with transaction(factory) as session:
            summary = await seed(session)
            invites = await issue_demo_invites(session)
            return summary, invites
    finally:
        # 不 dispose 的话进程退出时 asyncpg 会抱怨连接被 GC 掉，那个报错跟真正
        # 发生的事毫无关系，只会干扰下一个读日志的人。
        await engine.dispose()


def main() -> None:
    """`python -m adpilot.seed` 的入口。"""
    settings = get_settings()
    configure_logging(settings)

    if settings.is_production:
        raise SystemExit(
            "拒绝在 ENVIRONMENT=prod 下写入示例数据。\n"
            "这里没有 --force：往生产库灌假客户没有正当理由，留一个开关就等于没有"
            "这道护栏。\n"
            "确实要在类生产环境演示，先把 .env 里的 ENVIRONMENT 改掉。"
        )

    summary, invites = asyncio.run(_run(settings))

    # 每个客户一段：名字一行、码一行。中文名宽度不定，硬凑成一列对不齐。
    invite_lines = "\n".join(f"  {name}\n    {code}" for name, code in invites)
    # 拿最后一个客户举例：它就是那个暂停投放的账户所属的客户（见 _PLAN 的顺序）。
    idle_client, idle_code = invites[-1]

    # 这里用 print 不用 log：给人看的命令行回执，不该被 structlog 的 JSON 格式
    # 包起来 —— prod 下那套格式正是为机器准备的，而这个模块只在 dev 下跑。
    print(
        "示例数据就绪：\n"
        f"  客户    新建 {summary.clients_created}，已存在 {summary.clients_existing}\n"
        f"  账户    新建 {summary.accounts_created}，已存在 {summary.accounts_existing}\n"
        f"  日指标  新增 {summary.metrics_inserted} 行，已存在 {summary.metrics_existing} 行\n"
        f"  余额    新录 {summary.balances_recorded}，已存在 {summary.balances_existing}\n"
        "\n"
        "每个示例客户各一个邀请码（每次跑 seed 都新发，且只显示这一次）：\n"
        f"{invite_lines}\n"
        "\n"
        "接下来 —— 内部接口都要运营 token（账号在 .env 的 OPERATOR_USERNAME）：\n"
        "  TOKEN=$(curl -sX POST localhost:8000/api/auth/login \\\n"
        "      -H 'Content-Type: application/json' \\\n"
        '      -d \'{"username":"admin","password":"你设的密码"}\' | jq -r .token)\n'
        '  curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/clients\n'
        '  curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8000/api/alerts/sweep'
        "   # 应当得到 2 条告警\n"
        "\n"
        "客户那一侧不需要运营 token，拿其中任意一个码换一张 7 天的票：\n"
        f"  curl -sX POST localhost:8000/api/auth/redeem \\\n"
        "      -H 'Content-Type: application/json' \\\n"
        f'      -d \'{{"code":"{idle_code}"}}\'\n'
        "\n"
        f"上面这个码是「{idle_client}」的 —— 它的账户已暂停投放，拿它进客户端可以看到\n"
        "「可撑天数无定义」长什么样：那是「近期没花钱，算不出来」，不是「还能撑 0 天」。"
    )


if __name__ == "__main__":
    main()
