"""示例数据的测试。

这批数据最容易出的错不是「造不出来」—— 那种一跑就发现。**是造出来了但规则不认**：
`seed.py` 的 docstring 承诺「跑完恰好两条告警」，而随机波动、时区口径、阈值参数
任何一处偏一点，真实结局就变成一条、三条，或者一条都不报。而这恰恰是没人会去手工
核对的东西 —— 示例数据看起来"有内容"就会被当成对的。

所以这里的断言**不写成「金额等于某个字面量」**（那只在写下它的那天成立，而数据是
按今天滚动生成的），而是把 seed 造出来的数字喂进 `rules/` 里那两个真实的规则函数，
断言**结局**。阈值哪天调了，这些测试跟着一起说话。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot import seed
from adpilot.config import Environment, Settings
from adpilot.models.ad_account import AdAccount
from adpilot.models.llm_call import LLMCall
from adpilot.models.report import Report, ReportStatus
from adpilot.rules import anomaly as anomaly_rules
from adpilot.rules import balance as balance_rules
from adpilot.rules import stock as stock_rules
from adpilot.services import invite as invite_service

# 计划里那四个账户，按它们各自要演示的规则结局取名。
HEALTHY = "demo-meta-0001"
LOW_BALANCE = "demo-tiktok-0001"
CPA_SPIKE = "demo-meta-0002"
PAUSED = "demo-tiktok-0002"

_NUMERIC_COLUMNS = ("spend", "impressions", "clicks", "conversions", "revenue", "reach")

#: 库存快照序列的锚点。取哪个时刻不影响结论（规则看的是**两点之间的跨度**），
#: 所以写死一个而不是跟着 `now()` 走 —— 后者会让失败信息里的数字每天都不一样。
_LATEST_AT = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


def _accounts() -> Iterator[seed._Account]:
    for client in seed._PLAN:
        yield from client.accounts


def _products() -> Iterator[seed._Product]:
    for client in seed._PLAN:
        yield from client.products


def _stock_runway(plan: seed._Product) -> stock_rules.StockRunway:
    """把 seed 造的库存快照喂进真实的库存规则。

    **走的是和 `services/product.py` 一样的两条路**（文件自带的日均优先，没有就
    从快照序列推），所以这里的结论和实跑一次巡检得到的是同一个 —— 复制一份简化
    的判定逻辑进来，就等于让这条测试测它自己。
    """
    points = [stock_rules.StockPoint(captured_at=_LATEST_AT, qty=plan.qty)]
    if plan.previous_qty is not None:
        points.append(
            stock_rules.StockPoint(
                captured_at=_LATEST_AT - timedelta(days=plan.previous_days_ago),
                qty=plan.previous_qty,
            )
        )

    avg = plan.daily_sales
    if avg is None:
        avg = stock_rules.infer_daily_sales(points)
    return stock_rules.runway(plan.qty, avg)


def _account(external_id: str) -> seed._Account:
    for account in _accounts():
        if account.external_id == external_id:
            return account
    raise AssertionError(f"计划里没有这个账户：{external_id}")


def _runway(plan: seed._Account) -> balance_rules.BalanceRunway:
    """把 seed 造的余额和日均喂进真实的余额规则。"""
    yesterday = seed._yesterday(plan.timezone)
    available = seed._balance_amount(plan, yesterday)
    assert available is not None, f"{plan.external_id} 没有余额快照，测不了这条"
    return balance_rules.runway(available, seed._average_daily_spend(plan, yesterday))


def _anomaly(
    plan: seed._Account, metric: anomaly_rules.AnomalyMetric
) -> anomaly_rules.Anomaly | None:
    """把「昨天」和「上周同日」喂进真实的异动规则。"""
    yesterday = seed._yesterday(plan.timezone)
    baseline_day = yesterday - timedelta(days=anomaly_rules.COMPARISON_LAG_DAYS)

    # 第三个参数一律传真正的 yesterday：CPA 尖峰只在那一天生效，基线那天必须
    # 拿到未受干扰的数字，否则两边一起抬高，比率反而回到正常区间。
    current = seed._figures(plan, yesterday, yesterday)
    baseline = seed._figures(plan, baseline_day, yesterday)

    # CPA 在没有转化的那天是 None（无定义，不是 0），所以这个元组必须容得下 None。
    values: tuple[Decimal | None, Decimal | None]
    if metric is anomaly_rules.AnomalyMetric.SPEND:
        values = (current.spend, baseline.spend)
    else:
        values = (
            anomaly_rules.cost_per_action(current.spend, current.conversions),
            anomaly_rules.cost_per_action(baseline.spend, baseline.conversions),
        )

    return anomaly_rules.compare(
        metric,
        *values,
        current_spend=current.spend,
        baseline_spend=baseline.spend,
    )


# --- 数据本身立不立得住 -------------------------------------------------------


def test_campaign_shares_sum_to_one() -> None:
    """每个账户下的系列占比必须正好加起来是 1。

    差一点点的后果不是「少算一点」，而是系列级合计不等于账户级 —— 见下一条。
    """
    for plan in _accounts():
        total = sum((campaign.share for campaign in plan.campaigns), Decimal(0))
        assert total == 1, f"{plan.external_id} 的系列占比加起来是 {total}"


def test_campaign_rows_sum_to_account_row() -> None:
    """系列级六个数值列加起来，必须**精确等于**账户级那一行。

    逐份四舍五入天然会差几分钱，`_split_money` 用「最后一份吃余数」补掉了它。
    这条测试盯的就是那个补偿：账户级和系列级对不上，是翻明细的人最先发现、
    也最先失去信任的地方，而且它不会报错，只会让数字看起来"差不多对"。
    """
    for plan in _accounts():
        yesterday = seed._yesterday(plan.timezone)
        rows = seed._metric_rows(plan, yesterday, yesterday)
        account_row, campaign_rows = rows[0], rows[1:]

        assert len(campaign_rows) == len(plan.campaigns)
        for column in _NUMERIC_COLUMNS:
            total = sum(row[column] for row in campaign_rows)
            assert total == account_row[column], f"{plan.external_id} 的 {column} 对不上"


def test_figures_are_deterministic() -> None:
    """同一个 (账户, 日期) 反复算，结果必须一样。

    种子是按 `(账户, 日期)` 派生的，不是顺序消费一个全局 `Random` —— 顺序相关的
    随机数会让「昨天的数据今天跑和明天跑不一样」，那会让任何对着示例数据写的断言
    变成薛定谔的。
    """
    for plan in _accounts():
        yesterday = seed._yesterday(plan.timezone)
        for offset in (0, 3, 13):
            day = yesterday - timedelta(days=offset)
            assert seed._figures(plan, day, yesterday) == seed._figures(plan, day, yesterday)


def test_identifiers_are_obviously_fake() -> None:
    """客户名、账户 ID、系列 ID 必须一眼看出是假的。

    这是 CLAUDE.md 第一条硬规矩（真实客户名与广告账户 ID 不进仓库）的机器形态。
    往这张表里粘一个真实客户名是**不可逆**的：仓库是公开的，git 历史会留住它，
    真要清干净得重写每一个 commit hash。
    """
    for client in seed._PLAN:
        assert client.name.startswith("示例｜"), client.name
        for account in client.accounts:
            assert account.external_id.startswith("demo-"), account.external_id
            for campaign in account.campaigns:
                assert campaign.external_id.startswith("demo-"), campaign.external_id
        for product in client.products:
            # SKU 会在客户端和后台两处显示出来，真实商品编码同样不该进公开仓库。
            assert product.sku.startswith("demo-"), product.sku
            assert product.name.startswith("（示例）"), product.name


# --- 规则结局对不对 -----------------------------------------------------------


def test_low_balance_account_trips_the_rule() -> None:
    """余额告急那个账户必须真的触发告警，且天数落在阈值下方。"""
    verdict = _runway(_account(LOW_BALANCE))

    assert verdict.is_alerting
    assert verdict.days_left is not None
    assert verdict.days_left <= balance_rules.ALERT_THRESHOLD_DAYS


def test_healthy_account_stays_above_threshold() -> None:
    """正常账户不能告警 —— 而且要留出余量，不是刚好压线。

    压线的话，`_JITTER` 那点波动就足以让它在某些天翻过阈值，症状是「seed 出来的
    告警数量时多时少」。要求它至少是阈值的两倍，波动就再也够不着了。
    """
    verdict = _runway(_account(HEALTHY))

    assert not verdict.is_alerting
    assert verdict.days_left is not None
    assert verdict.days_left > balance_rules.ALERT_THRESHOLD_DAYS * 2


def test_paused_account_has_undefined_runway() -> None:
    """暂停账户：可撑天数**无定义**，且不告警。

    这是整批数据里最有价值的一条。「没有消耗就不会归零」这条边界最容易被写成
    「0 天，立刻告警」，而那种告警会让人对整个告警列表脱敏 —— 有一份示例数据
    天天覆盖着它，比一句注释管用。
    """
    verdict = _runway(_account(PAUSED))

    assert verdict.days_left is None, "日均为 0 时可撑天数是无定义，不是 0"
    assert not verdict.is_alerting
    assert verdict.available > 0, "暂停不等于没钱：余额得是个正数，否则演示的是另一回事"


def test_spike_account_trips_cpa_anomaly() -> None:
    """CPA 尖峰那个账户必须触发 CPA 异动，方向朝上。"""
    verdict = _anomaly(_account(CPA_SPIKE), anomaly_rules.AnomalyMetric.CPA)

    assert verdict is not None
    assert verdict.is_anomalous
    assert verdict.direction is anomaly_rules.AnomalyDirection.UP
    assert verdict.change_ratio > anomaly_rules.CHANGE_THRESHOLD


def test_spike_account_does_not_trip_spend_anomaly() -> None:
    """同一个账户**不能**同时报花费异动。

    尖峰只抬 CPA、不动 `spend`（花一样的钱、转化少了一半）。这条测试守的是
    「恰好一条告警」：如果哪天有人顺手把 spend 也调高，这个账户会变成两条，
    而 seed 的 docstring 承诺的是两条**总数**。
    """
    verdict = _anomaly(_account(CPA_SPIKE), anomaly_rules.AnomalyMetric.SPEND)

    assert verdict is None or not verdict.is_anomalous


@pytest.mark.parametrize("external_id", [HEALTHY, LOW_BALANCE])
def test_steady_accounts_have_no_anomaly(external_id: str) -> None:
    """平稳账户两个指标都不能异动 —— 随机波动不该够得着阈值。

    `_JITTER` 是 10，周同比最坏是 1.1/0.9 ≈ +22%，离 40% 还有距离。这条测试是那个
    推算的机器形态：把 `_JITTER` 调到 18 左右就开始偶发失败，而那正是它该失败的
    时候。
    """
    plan = _account(external_id)
    for metric in anomaly_rules.AnomalyMetric:
        verdict = _anomaly(plan, metric)
        assert verdict is None or not verdict.is_anomalous, f"{external_id} 的 {metric} 异动了"


def test_plan_produces_exactly_three_alerts() -> None:
    """整批数据跑完规则，恰好三条告警 —— `seed.py` 的 docstring 就是这么承诺的。

    单独测每个账户/商品还不够：这条守的是**总数**。加一个新的示例对象时，如果它
    顺带触发了什么，这里会红，逼着人回去把那份 docstring 里的表格、README 里的
    数字和命令行回执一起改掉 —— 那个数字写在四个地方，而这是唯一会拦住人的一个。
    """
    alerting = 0
    for plan in _accounts():
        if (
            seed._balance_amount(plan, seed._yesterday(plan.timezone)) is not None
            and _runway(plan).is_alerting
        ):
            alerting += 1
        for metric in anomaly_rules.AnomalyMetric:
            verdict = _anomaly(plan, metric)
            if verdict is not None and verdict.is_anomalous:
                alerting += 1

    for product in _products():
        if _stock_runway(product).is_alerting:
            alerting += 1

    assert alerting == 3


def test_the_stock_products_cover_all_three_sales_sources() -> None:
    """🔴 三个示例商品必须各演示一种日均来源，一个都不能少。

    `sales_source` 这个字段在界面上决定显示哪句提示（「来自店铺导出」/「按库存
    变化推算」/「再导一次就能算了」），而只有三种值都出现过，那几句提示才有人
    看得到。少一种的症状是「某个分支的文案从没被人看过」，而那种文案最容易写错。
    """
    verdicts = {product.sku: _stock_runway(product) for product in _products()}

    from_file = verdicts["demo-sku-0001"]
    assert from_file.avg_daily_sales == Decimal(6), "这个款的日均该直接来自导出文件"
    assert from_file.is_alerting

    inferred = verdicts["demo-sku-0002"]
    assert inferred.avg_daily_sales is not None, "两条快照就该推得出日均"
    assert not inferred.is_alerting

    unknown = verdicts["demo-sku-0003"]
    assert unknown.avg_daily_sales is None, "只有一条快照时日均必须是「算不出来」"
    assert unknown.days_left is None
    assert not unknown.is_alerting, "算不出来 ≠ 已断货"


# --- 护栏 ---------------------------------------------------------------------


def test_seed_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ENVIRONMENT=prod` 必须拒绝执行，而且要在连数据库之前就拒绝。

    没有 `--force` 是刻意的，所以这里也没有「force 能绕过」的对应用例 —— 真加了
    那个开关，这条测试仍然绿，但护栏已经没了。发现它只能靠 review。
    """
    settings = Settings(
        environment=Environment.PROD,
        postgres_password=SecretStr("x"),
        mongo_password=SecretStr("x"),
        rabbitmq_password=SecretStr("x"),
        # 生产环境的 Settings 现在还要求认证配齐（config.py 的
        # `_require_auth_in_production`），否则**构造这一步**就过不去。
        auth_secret=SecretStr("x" * 32),
        operator_password_hash=SecretStr("x"),
    )
    monkeypatch.setattr(seed, "get_settings", lambda: settings)
    monkeypatch.setattr(seed, "configure_logging", lambda _: None)

    with pytest.raises(SystemExit) as raised:
        seed.main()

    assert "prod" in str(raised.value)


@pytest.mark.integration
async def test_the_demo_invite_codes_are_random_and_actually_work(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """seed 发出来的邀请码必须是**随机的**，且每个都真的能换到对应那个客户。

    写死一个「demo 码」等于往公开仓库里放一把人人皆知的钥匙 —— 哪怕它只对示例
    数据有效，也会有人把 seed 跑在一个已经有真实客户的库上。两次调用拿到同一个
    码，就是那件事发生了。

    **每个客户都要有码**：四个示例账户各演示一种规则结局，而客户端一张票只能看
    一个客户。少发一个，那个客户想演示的边界在界面上就永远看不到。
    """
    await seed.seed(live_session, live_settings)

    first = await seed.issue_demo_invites(live_session)
    second = await seed.issue_demo_invites(live_session)

    assert len(first) == len(seed._PLAN), "有客户没拿到码"
    assert len(first) == len(second)

    codes = [code for _, code in first]
    assert len(set(codes)) == len(codes), "同一批里发出了重复的码"
    assert all(len(code) >= 32 for code in codes), "码太短，熵不够 —— 这是个不需要认证就能试的入口"

    for (_, before), (_, after) in zip(first, second, strict=True):
        assert before != after, "两次发出同一个码，说明它被写死在源码里了"

    # 每个码都得换到**它自己那个**客户 —— 串了的话，客户端会把 A 的花费给 B 看。
    for name, code in first:
        redeemed = await invite_service.redeem(live_session, code)
        assert redeemed.name == name


@pytest.mark.integration
async def test_seed_publishes_a_demo_report_without_ever_calling_the_model(
    live_session: AsyncSession,
    live_settings: Settings,
) -> None:
    """seed 造出的日报是**已发布**的，而且整个过程**一次模型都没调**。

    两件事合在一条里验，因为它们是同一个承诺的两面：

    * demo 里要看得到日报 —— 那是这套系统最值钱的产出，示例数据里没有它，等于最
      重要的东西没展示；
    * 🔴 **灌一次示例数据不该花钱。** 这里故意把 LLM 配上再跑 seed：哪怕使用者
      真配了 `LLM_BASE_URL`，`llm_calls` 里也必须一行都不多 —— 悄悄花掉几笔钱是
      任何人都不会预期的行为。

    顺带这也每次都验一遍那两条发布校验（人工修订过、操作记录非空）：seed 走的是
    真链路，两条里少一条它就发不出去、这条用例就红。
    """
    configured = live_settings.model_copy(
        update={
            "llm_base_url": "http://llm.invalid/v1",
            "llm_model": "should-never-be-called",
        }
    )
    assert configured.llm_is_configured

    calls_before = await live_session.scalar(select(func.count(LLMCall.id)))
    await seed.seed(live_session, configured)
    calls_after = await live_session.scalar(select(func.count(LLMCall.id)))

    assert calls_after == calls_before, "seed 调用了 LLM —— 灌示例数据不该花钱"

    # 🔴 按计划逐个账户去找，**不数全表**。数全表要求库是空的，而 seed 恰恰是
    # 「只添不改、重复跑安全」的 —— 在一个跑过几天 seed 的开发库上，全表里还躺着
    # 前几天那几份日报，于是这条断言在第二天就红，而红的原因跟被测的行为无关。
    for client_plan in seed._PLAN:
        for account_plan in client_plan.accounts:
            account = await live_session.scalar(
                select(AdAccount).where(AdAccount.external_id == account_plan.external_id)
            )
            assert account is not None, f"seed 没建出账户：{account_plan.external_id}"
            report = await live_session.scalar(
                select(Report).where(
                    Report.account_id == account.id,
                    Report.stat_date == seed._yesterday(account_plan.timezone),
                )
            )
            assert report is not None, f"{account_plan.external_id} 昨天那份日报没发出来"
            _assert_demo_report(report)


def _assert_demo_report(report: Report) -> None:
    """一份 seed 造出来的日报该长什么样。"""
    assert report.status is ReportStatus.PUBLISHED
    # 人话是示例文案，**不是模型写的** —— 没调模型，原文自然是空的
    assert report.narrative is not None
    assert report.llm_narrative is None
    assert report.llm_call_id is None
    # 发布校验之一：本期做了什么不能是空的
    assert report.actions_snapshot, "日报里没有操作记录，它本不该发得出去"
