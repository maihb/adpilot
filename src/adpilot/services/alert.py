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
from adpilot.models.ad_account import AdAccount
from adpilot.models.alert import Alert, AlertKind, AlertStatus
from adpilot.notifiers import webhook
from adpilot.rules import anomaly as anomaly_rules
from adpilot.services import balance as balance_service
from adpilot.services import daily_metric as daily_metric_service

log = structlog.get_logger(__name__)

#: 余额告警在一个账户里只有一件事，`subject` 是个常量。
BALANCE_SUBJECT = "balance"


@dataclass(frozen=True, slots=True)
class Finding:
    """「此刻有这么个问题」。还不是告警 —— 对账之后才知道它是新开的还是老的。"""

    kind: AlertKind
    subject: str
    message: str
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """一轮巡检的结果。数字都是**告警条数**，不是账户数。"""

    accounts: int
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
            account_id=account.id,
            findings=findings,
            now=now,
        )
        opened.extend(account_opened)
        still_open += account_still
        resolved += account_resolved

    # 先把行写进去再推送：推送失败时告警**已经在库里了**，人打开清单还是看得到，
    # 而 notified_at 留空意味着下一轮会再试一次。反过来（先推后写）则是推成功了
    # 但库里没有 —— 那条告警从此只存在于某个聊天窗口里。
    await session.flush()
    notified = await _notify(settings, opened, now)

    summary = SweepSummary(
        accounts=len(accounts),
        opened=len(opened),
        still_open=still_open,
        resolved=resolved,
        notified=notified,
    )
    log.info(
        "alert_sweep_finished",
        accounts=summary.accounts,
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
    account_id: int,
    findings: Sequence[Finding],
    now: datetime,
) -> tuple[list[Alert], int, int]:
    """把一个账户的判定对账进表，返回 (新开的, 仍然开着的, 刚解决的)。"""
    open_rows = (
        await session.scalars(
            select(Alert).where(
                Alert.account_id == account_id,
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
