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
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from adpilot import seed
from adpilot.config import Environment, Settings
from adpilot.rules import anomaly as anomaly_rules
from adpilot.rules import balance as balance_rules

# 计划里那四个账户，按它们各自要演示的规则结局取名。
HEALTHY = "demo-meta-0001"
LOW_BALANCE = "demo-tiktok-0001"
CPA_SPIKE = "demo-meta-0002"
PAUSED = "demo-tiktok-0002"

_NUMERIC_COLUMNS = ("spend", "impressions", "clicks", "conversions", "revenue", "reach")


def _accounts() -> Iterator[seed._Account]:
    for client in seed._PLAN:
        yield from client.accounts


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


def test_plan_produces_exactly_two_alerting_accounts() -> None:
    """整批数据跑完规则，恰好两条告警 —— `seed.py` 的 docstring 就是这么承诺的。

    单独测每个账户还不够：这条守的是**总数**。加一个新的示例账户时，如果它顺带
    触发了什么，这里会红，逼着人回去把那份 docstring 里的表格一起改掉。
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

    assert alerting == 2


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
    )
    monkeypatch.setattr(seed, "get_settings", lambda: settings)
    monkeypatch.setattr(seed, "configure_logging", lambda _: None)

    with pytest.raises(SystemExit) as raised:
        seed.main()

    assert "prod" in str(raised.value)
