"""告警巡检：把规则算出来的判定，对账成 `alerts` 表里的状态。

## 一轮巡检做三件事

对每个在投账户，先算出「此刻有哪些问题」（`Finding`），再和库里那些 `open` 的行
对账：

| 情况 | 动作 |
|---|---|
| 现在有、库里没有 | **开**一条，并尝试推送 |
| 现在有、库里也有 | 只更新 `last_seen_at` 和当时的数字，**不重复推送** |
| 现在没有、库里有 | 置成 `resolved` |

「不重复推送」是这整套状态机存在的理由。巡检每小时跑一次，一个持续三天的余额问题
会被发现七十多次 —— 每次都推的话，人第一天就会把这个通知静音，然后错过真正新出现
的那一条。

## 为什么不在这里判断「要不要告警」

判断在 `rules/`：那边是纯函数，一张表格式的参数化测试就能覆盖完边界（阈值含不含
等号、分母为 0 怎么办）。这一层只做两件 `rules/` 做不了的事 —— **查库**，和**对账**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adpilot.config import Settings
from adpilot.llm import contracts as llm_contracts
from adpilot.llm import prompts
from adpilot.llm.base import LLMProvider
from adpilot.models.ad_account import AdAccount
from adpilot.models.alert import Alert, AlertKind, AlertStatus
from adpilot.models.client import Client
from adpilot.models.llm_call import LLMCall
from adpilot.notifiers import webhook
from adpilot.rules import anomaly as anomaly_rules
from adpilot.services import action as action_service
from adpilot.services import ad_account as ad_account_service
from adpilot.services import balance as balance_service
from adpilot.services import daily_metric as daily_metric_service
from adpilot.services import llm as llm_service
from adpilot.services import product as product_service
from adpilot.services.exceptions import NotFoundError

log = structlog.get_logger(__name__)

#: 余额告警在一个账户里只有一件事，`subject` 是个常量。
BALANCE_SUBJECT = "balance"

#: 库存告警每个商品一件事，`subject` 带上 SKU。没有它，同一个客户的两个断货商品
#: 会互相顶掉 —— 部分唯一索引认的是 (客户, 种类, subject)。
STOCK_SUBJECT_PREFIX = "stock:"


@dataclass(frozen=True, slots=True)
class Finding:
    """「此刻有这么个问题」。还不是告警 —— 对账之后才知道它是新开的还是老的。"""

    kind: AlertKind
    subject: str
    message: str
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """一轮巡检的结果。`opened` 那几个数字都是**告警条数**，不是账户数。"""

    accounts: int

    #: 这一轮看了几个客户的库存。**不等于客户总数** —— 一个在投账户都没有的客户
    #: 整个跳过（`services/product.py` 的 `alerts` 讲了为什么）。
    clients: int

    opened: int
    still_open: int
    resolved: int
    notified: int


async def sweep(session: AsyncSession, settings: Settings) -> SweepSummary:
    """巡检一遍所有在投账户。

    **可以反复跑**：对账是幂等的，同一件事跑十次也只有一条 `open`。这是它敢挂在
    每小时的定时任务上、也敢在失败后重试的前提。

    停投的账户（`is_active=false`）不看，理由同余额清单：它们混在告警里只会让人
    学会忽略这个列表。已经开着的告警**不会**因为账户停投而自动 resolve —— 那需要
    一次显式的巡检确认问题不在了，而不是靠「看不见了」。

    ### 两趟，因为告警有两个层级

    余额和指标异动挂在**账户**上，库存断货挂在**客户**上（商品是店铺的属性，
    一个客户的多个投放账户推的是同一批货）。所以对账要分两趟走，各自按自己的
    去重键 —— 混成一趟的话，库存那条要么被账户数放大成 N 条，要么因为
    `NULL != NULL` 而每轮新开一条（`models/alert.py` 的类 docstring）。
    """
    accounts = (await session.scalars(select(AdAccount).where(AdAccount.is_active.is_(True)))).all()

    now = datetime.now(UTC)
    opened: list[Alert] = []
    still_open = 0
    resolved = 0

    for account in accounts:
        findings = await _findings_for(session, account)
        account_opened, account_still, account_resolved = await _reconcile(
            session,
            client_id=account.client_id,
            account_id=account.id,
            findings=findings,
            now=now,
        )
        opened.extend(account_opened)
        still_open += account_still
        resolved += account_resolved

    stock_alerts = await product_service.alerts(session, only_alerting=True)
    by_client: dict[int, list[Finding]] = {}
    for item in stock_alerts:
        by_client.setdefault(item.client_id, []).append(_stock_finding(item))

    # 🔴 遍历的是**被巡检过的客户**，不是有断货商品的客户。少了这一步，一个客户
    # 的库存补上之后那条告警永远不会被 resolve —— 它不再出现在 findings 里，而
    # 「不再出现」恰恰是对账要看见的东西。
    swept_clients = {account.client_id for account in accounts}
    for client_id in sorted(swept_clients):
        client_opened, client_still, client_resolved = await _reconcile(
            session,
            client_id=client_id,
            account_id=None,
            findings=by_client.get(client_id, []),
            now=now,
        )
        opened.extend(client_opened)
        still_open += client_still
        resolved += client_resolved

    # 先把行写进去再推送：推送失败时告警**已经在库里了**，人打开清单还是看得到，
    # 而 notified_at 留空意味着下一轮会再试一次。反过来（先推后写）则是推成功了
    # 但库里没有 —— 那条告警从此只存在于某个聊天窗口里。
    await session.flush()
    notified = await _notify(settings, opened, now)

    summary = SweepSummary(
        accounts=len(accounts),
        clients=len(swept_clients),
        opened=len(opened),
        still_open=still_open,
        resolved=resolved,
        notified=notified,
    )
    log.info(
        "alert_sweep_finished",
        accounts=summary.accounts,
        clients=summary.clients,
        opened=summary.opened,
        still_open=summary.still_open,
        resolved=summary.resolved,
        notified=summary.notified,
    )
    return summary


async def list_alerts(
    session: AsyncSession,
    *,
    status: AlertStatus | None = None,
    account_id: int | None = None,
    page: int,
    page_size: int,
) -> tuple[Sequence[Alert], int]:
    """分页列出告警，未解决的在前、同状态下新的在前。

    排序把 `open` 顶到最前面，而不是单纯按时间倒序：这张表既是待办清单也是历史，
    而打开它的人九成是来看待办的。
    """
    filters = []
    if status is not None:
        filters.append(Alert.status == status.value)
    if account_id is not None:
        filters.append(Alert.account_id == account_id)

    total = await session.scalar(select(func.count(Alert.id)).where(*filters))
    rows = await session.scalars(
        select(Alert)
        .where(*filters)
        .order_by(
            case((Alert.status == AlertStatus.OPEN.value, 0), else_=1),
            Alert.opened_at.desc(),
            Alert.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def list_for_client(
    session: AsyncSession,
    *,
    client_id: int,
    only_open: bool,
    page: int,
    page_size: int,
) -> tuple[Sequence[Alert], int]:
    """客户端那条路径上的告警清单。`client_id` 必填，不给根本调不通。

    过滤是 `alerts.client_id` 上的一个等值条件。**D16 之前这里是一次 JOIN 回
    `ad_accounts`**，改掉是因为库存告警的 `account_id` 是 NULL（客户级），
    内连接会把它整个筛掉 —— 客户于是永远看不到自己的断货告警，而且不会有任何
    报错。少一次 JOIN 顺带也少一个能漏掉作用域的地方（CLAUDE.md 硬规矩 4）。

    **不接受 `account_id` 入参** —— 客户要的是「我这边有什么要注意的」，而按账户
    筛这件事会多出一个需要校验归属的入口，收益却只是少几行。

    默认只给未解决的：客户看的是当下，不是台账。历史（含已解决）要显式要。
    """
    filters = [Alert.client_id == client_id]
    if only_open:
        filters.append(Alert.status == AlertStatus.OPEN.value)

    total = await session.scalar(select(func.count(Alert.id)).where(*filters))
    rows = await session.scalars(
        select(Alert)
        .where(*filters)
        .order_by(
            case((Alert.status == AlertStatus.OPEN.value, 0), else_=1),
            Alert.opened_at.desc(),
            Alert.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def _findings_for(session: AsyncSession, account: AdAccount) -> list[Finding]:
    findings: list[Finding] = []

    balance_finding = await _balance_finding(session, account)
    if balance_finding is not None:
        findings.append(balance_finding)

    findings.extend(await _anomaly_findings(session, account))
    return findings


async def _balance_finding(session: AsyncSession, account: AdAccount) -> Finding | None:
    alert = await balance_service.alert_for_account(session, account.id)
    if alert is None or not alert.runway.is_alerting:
        return None

    days_left = alert.runway.days_left
    return Finding(
        kind=AlertKind.BALANCE_LOW,
        subject=BALANCE_SUBJECT,
        # 人话摘要由这里拼，不交给 LLM：这是规则算出来的事实，不是解释
        # （设计文档第五节的边界）。
        message=(
            f"余额只够撑 {days_left} 天"
            f"（还剩 {alert.runway.available} {alert.currency}，"
            f"近期日均 {alert.runway.avg_daily_spend}）"
        ),
        detail={
            "days_left": _plain(days_left),
            "available": _plain(alert.runway.available),
            "avg_daily_spend": _plain(alert.runway.avg_daily_spend),
            "threshold_days": _plain(alert.runway.threshold_days),
            "currency": alert.currency,
            "captured_at": alert.captured_at.isoformat(),
            "lookback_from": alert.lookback_from.isoformat(),
            "lookback_to": alert.lookback_to.isoformat(),
            "days_with_data": alert.days_with_data,
        },
    )


def _stock_finding(item: product_service.StockAlert) -> Finding:
    """把一个商品的断货判定拼成一条告警。

    人话摘要里**带上日均是怎么来的**：推算出来的日均建立在「中间没补过货」这个
    假设上，而人看到「还能撑 2 天」时第一个该问的就是这个数可信不可信。把它藏进
    `detail` 等于没写 —— 推送到群里的只有 `message` 这一行。
    """
    days_left = item.runway.days_left
    label = item.name or item.sku
    source = (
        "店铺导出" if item.sales_source == product_service.SALES_FROM_FILE else "按库存变化推算"
    )
    return Finding(
        kind=AlertKind.STOCK_LOW,
        subject=f"{STOCK_SUBJECT_PREFIX}{item.sku}",
        message=(
            f"{label}（{item.sku}）库存只够撑 {days_left} 天"
            f"（还剩 {item.runway.stock_qty}，日均销量 {item.runway.avg_daily_sales}，{source}）"
        ),
        detail={
            "days_left": _plain(days_left),
            "stock_qty": _plain(item.runway.stock_qty),
            "avg_daily_sales": _plain(item.runway.avg_daily_sales),
            "threshold_days": _plain(item.runway.threshold_days),
            "sku": item.sku,
            "product_name": item.name,
            "product_id": item.product_id,
            "sales_source": item.sales_source,
            "captured_at": item.captured_at.isoformat(),
            "snapshot_count": item.snapshot_count,
        },
    )


async def _anomaly_findings(session: AsyncSession, account: AdAccount) -> list[Finding]:
    """昨天 vs 上周同一天。缺任何一天的数据就整个跳过 —— 不拿凑合的基线硬判。"""
    yesterday = _yesterday(account)
    baseline_day = yesterday - timedelta(days=anomaly_rules.COMPARISON_LAG_DAYS)

    totals = await daily_metric_service.totals_on_days(
        session,
        account_id=account.id,
        days=[yesterday, baseline_day],
    )
    current = totals.get(yesterday)
    baseline = totals.get(baseline_day)
    if current is None or baseline is None:
        return []

    candidates = {
        anomaly_rules.AnomalyMetric.SPEND: (current.spend, baseline.spend),
        anomaly_rules.AnomalyMetric.CPA: (
            anomaly_rules.cost_per_action(current.spend, current.conversions),
            anomaly_rules.cost_per_action(baseline.spend, baseline.conversions),
        ),
    }

    findings: list[Finding] = []
    for metric, (now_value, then_value) in candidates.items():
        verdict = anomaly_rules.compare(
            metric,
            now_value,
            then_value,
            current_spend=current.spend,
            baseline_spend=baseline.spend,
        )
        if verdict is None or not verdict.is_anomalous:
            continue

        percent = (verdict.change_ratio * 100).quantize(Decimal("0.1"))
        moved = "上升" if verdict.direction is anomaly_rules.AnomalyDirection.UP else "下降"
        findings.append(
            Finding(
                kind=AlertKind.METRIC_ANOMALY,
                # subject 带上指标名：没有它，花费异动和 CPA 异动会互相顶掉，
                # 因为部分唯一索引认的是 (账户, 种类, subject)。
                subject=f"metric:{metric.value}",
                message=(
                    f"{metric.value} 较上周同日{moved} {abs(percent)}%"
                    f"（{verdict.baseline} → {verdict.current}）"
                ),
                detail={
                    "metric": metric.value,
                    "current": _plain(verdict.current),
                    "baseline": _plain(verdict.baseline),
                    "change_ratio": _plain(verdict.change_ratio),
                    "direction": verdict.direction.value,
                    "threshold": _plain(verdict.threshold),
                    "stat_date": yesterday.isoformat(),
                    "baseline_date": baseline_day.isoformat(),
                    "currency": account.currency,
                },
            )
        )
    return findings


async def _reconcile(
    session: AsyncSession,
    *,
    client_id: int,
    account_id: int | None,
    findings: Sequence[Finding],
    now: datetime,
) -> tuple[list[Alert], int, int]:
    """把一组判定对账进表，返回 (新开的, 仍然开着的, 刚解决的)。

    `account_id` 为 `None` 时对的是**客户级**那一批（目前只有库存断货）。两种
    情况用的是两个不同的去重键，所以下面那句 `where` 的条件也不同 ——
    `Alert.account_id == None` 在 SQLAlchemy 里会渲染成 `IS NULL`，但写成
    `.is_(None)` 才不会被 lint 挑，也才读得出意图。
    """
    scope = Alert.account_id.is_(None) if account_id is None else Alert.account_id == account_id
    open_rows = (
        await session.scalars(
            select(Alert).where(
                Alert.client_id == client_id,
                scope,
                Alert.status == AlertStatus.OPEN.value,
            )
        )
    ).all()
    by_key = {(row.kind, row.subject): row for row in open_rows}

    opened: list[Alert] = []
    still_open = 0
    for finding in findings:
        existing = by_key.pop((finding.kind.value, finding.subject), None)
        if existing is None:
            alert = Alert(
                client_id=client_id,
                account_id=account_id,
                kind=finding.kind.value,
                status=AlertStatus.OPEN.value,
                subject=finding.subject,
                message=finding.message,
                detail=finding.detail,
                opened_at=now,
                last_seen_at=now,
            )
            session.add(alert)
            opened.append(alert)
            continue

        # 仍然成立：刷新「问题还在」的时刻和当时的数字。**不动 `opened_at`** ——
        # 「这个问题从什么时候开始的」是日报里要写的东西。
        existing.last_seen_at = now
        existing.message = finding.message
        existing.detail = finding.detail
        still_open += 1

    # 剩在 by_key 里的，是这一轮没再出现的 —— 问题不在了。
    for stale in by_key.values():
        stale.status = AlertStatus.RESOLVED.value
        stale.resolved_at = now

    return opened, still_open, len(by_key)


async def _notify(settings: Settings, opened: Sequence[Alert], now: datetime) -> int:
    """推送新开的告警，返回推成功的条数。

    没配 webhook 就整个跳过，只留一条日志 —— 这是默认状态，不是故障。
    """
    if not opened:
        return 0
    if not settings.alerts_are_pushed:
        log.info("alert_webhook_not_configured", pending=len(opened))
        return 0

    sent = 0
    for alert in opened:
        ok = await webhook.send(
            settings.alert_webhook_url,
            {
                "kind": alert.kind,
                "account_id": alert.account_id,
                "subject": alert.subject,
                "message": alert.message,
                "detail": alert.detail,
                "opened_at": alert.opened_at.isoformat(),
            },
        )
        if ok:
            # 推成功才盖戳。失败的留空，下一轮巡检会把它当成「还没通知过」再试
            # 一次 —— 自愈，不需要单独的重试队列。
            alert.notified_at = now
            sent += 1
    return sent


def _yesterday(account: AdAccount) -> date:
    """账户时区下的昨天。

    用服务器时区会在日切点附近整体差一天，而差一天的「上周同日」就不是同一个
    星期几了 —— 周同比抵消星期几效应的全部意义就没了。
    """
    return datetime.now(ZoneInfo(account.timezone)).date() - timedelta(days=1)


def _plain(value: Decimal | None) -> str | None:
    """`Decimal` → 字符串，供 JSONB 存储。

    **不转 float。** `detail` 里全是金额和比率，而 conventions.md 那条「金额一律
    Decimal、JSON 里是字符串」在这里同样适用 —— 落进 JSONB 的 float 读回来就已经
    不是原来那个数了。
    """
    return None if value is None else str(value)


#: 诊断时往前看几天的操作记录。7 天盖住一个完整的周内/周末周期，也盖得住「上周
#: 五调了预算、这周一才显出来」这种滞后 —— 而异动的原因十有八九就在这段操作里。
DIAGNOSIS_LOOKBACK_DAYS = 7


@dataclass(frozen=True, slots=True)
class DiagnosisOutcome:
    """一次诊断的结果。

    `diagnosis is None` 表示模型这次没答上来（挂了，或者连着几次输出都不合格）。
    **这不是异常** —— 告警本身、以及它带的那些数字全都还在，诊断只是锦上添花。
    """

    diagnosis: llm_contracts.Diagnosis | None

    #: 这次调用的记账行。失败时靠它去 `llm_calls` 查为什么。
    call: LLMCall


async def diagnose(
    session: AsyncSession,
    settings: Settings,
    *,
    alert_id: int,
    provider: LLMProvider | None = None,
) -> DiagnosisOutcome:
    """解释一条告警：**大概率**是什么原因、接下来该核实什么。

    🔴 **不自动调用，运营点一下才诊断**（[设计][d]第七节）。理由是告警的构成：
    大部分一眼就知道原因（余额低了 → 充钱；花费涨了 → 昨天调过预算），给每条都
    自动花一次钱，钱就花在了不需要解释的那些上面 —— 而诊断的价值恰恰在难解释的
    少数。

    输出里**没有任何「把预算改成 X」这样的字段**（`llm/contracts.py` 的
    `Diagnosis`），那是设计文档第五节第 1 条那条硬边界的形态：它给方向，不给指令，
    更不会有人替它执行。

    [d]: ../../../docs/design/2026-08-21-llm-reports.md
    """
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError(f"告警不存在：{alert_id}")

    # 🔴 客户级告警（库存断货）没有账户，于是也**不带操作记录**。
    #
    # 这不是省事：断货的原因在店铺那一侧（卖得比预期快、或者没按时补货），而操作
    # 记录里全是投放动作（调预算、换素材）。把它们塞进去只会诱导模型把两件无关
    # 的事说成因果 —— 而那种解释读起来最像真的。账户名这一格改用客户名，因为
    # 提示词要的是「这是谁的事」。
    account = (
        None
        if alert.account_id is None
        else await ad_account_service.get(session, alert.account_id)
    )

    actions: Sequence[Any] = []
    if account is not None:
        end = _yesterday(account)
        actions = await action_service.list_in_window(
            session,
            account=account,
            start=end - timedelta(days=DIAGNOSIS_LOOKBACK_DAYS - 1),
            end=end,
        )
        subject_name = account.name
    else:
        client = await session.get(Client, alert.client_id)
        subject_name = client.name if client is not None else f"客户 {alert.client_id}"

    outcome = await llm_service.run(
        session,
        settings,
        prompt=prompts.DIAGNOSIS,
        payload=llm_contracts.DiagnosisInput(
            account_name=subject_name,
            alert_kind=alert.kind,
            alert_message=alert.message,
            context=_diagnosis_context(alert),
            actions=[
                llm_contracts.ActionLine(
                    performed_at=row.performed_at.isoformat(),
                    summary=row.summary,
                    reason=row.reason,
                )
                for row in actions
            ],
        ),
        output_type=llm_contracts.Diagnosis,
        account_id=alert.account_id,
        provider=provider,
    )

    log.info(
        "alert_diagnosed",
        alert_id=alert_id,
        kind=alert.kind,
        account_id=alert.account_id,
        answered=outcome.output is not None,
    )
    return DiagnosisOutcome(diagnosis=outcome.output, call=outcome.call)


def _diagnosis_context(alert: Alert) -> list[llm_contracts.MetricLine]:
    """把 `alert.detail` 里那些数字翻译成模型看得懂的几行。

    按 `kind` 分支而不是把 JSONB 整个倒给模型：`detail` 的键是英文缩写、值是裸
    字符串，直接扔过去等于要求模型自己猜口径 —— 而它会猜，还会猜错。这一步是
    显式的翻译（同 `services/report.py` 的 `_metric_lines`）。

    认不出的种类给一行原始摘要就好，**不猜**：新加一种告警时这里没跟上，最坏的
    结果应该是「诊断得笼统」，不是「诊断得煞有介事但是错的」。
    """
    detail = alert.detail

    if alert.kind == AlertKind.BALANCE_LOW.value:
        return [
            llm_contracts.MetricLine(label="可撑天数", value=str(detail.get("days_left"))),
            llm_contracts.MetricLine(
                label="可用余额",
                value=f"{detail.get('available')} {detail.get('currency', '')}".strip(),
            ),
            llm_contracts.MetricLine(
                label="近期日均消耗", value=str(detail.get("avg_daily_spend"))
            ),
            llm_contracts.MetricLine(
                label="告警阈值（天）", value=str(detail.get("threshold_days"))
            ),
        ]

    if alert.kind == AlertKind.STOCK_LOW.value:
        return [
            llm_contracts.MetricLine(label="商品", value=f"{detail.get('product_name') or ''}"),
            llm_contracts.MetricLine(label="商品编码", value=str(detail.get("sku"))),
            llm_contracts.MetricLine(label="可撑天数", value=str(detail.get("days_left"))),
            llm_contracts.MetricLine(label="剩余库存", value=str(detail.get("stock_qty"))),
            llm_contracts.MetricLine(
                label="日均销量",
                value=str(detail.get("avg_daily_sales")),
                # 🔴 这一行是给模型的**可信度提示**，不是装饰：推算出来的日均建立
                # 在「中间没补过货」这个假设上，模型该知道它拿到的是哪一种。
                change=(
                    "来自店铺导出"
                    if detail.get("sales_source") == product_service.SALES_FROM_FILE
                    else "由库存变化推算，中间若补过货会偏高"
                ),
            ),
            llm_contracts.MetricLine(
                label="告警阈值（天）", value=str(detail.get("threshold_days"))
            ),
        ]

    if alert.kind == AlertKind.METRIC_ANOMALY.value:
        return [
            llm_contracts.MetricLine(label="指标", value=str(detail.get("metric"))),
            llm_contracts.MetricLine(
                label="当期值",
                value=str(detail.get("current")),
                change=f"较上周同日 {detail.get('direction')}",
            ),
            llm_contracts.MetricLine(label="上周同日", value=str(detail.get("baseline"))),
            llm_contracts.MetricLine(label="对照日", value=str(detail.get("baseline_date"))),
        ]

    return [llm_contracts.MetricLine(label="告警摘要", value=alert.message)]
