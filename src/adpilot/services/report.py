"""日报：生成、人工修订、发布。

## 这一层的形状为什么是这样

一次生成分成**两段**，中间那条缝是刻意的：

1. **算数字、取操作记录和告警、落库** —— 全是确定性代码，不碰 LLM；
2. **让模型写那一行人话** —— 可能失败，失败了第 1 段照样留在库里。

设计文档第四节那条「LLM 失败不阻塞」就是这条缝：数字部分不该被模型的可用性绑架，
人话字段留空并标注「未生成」，人可以自己写。

## 数字在**生成**那一刻固定

不是发布那一刻。理由是**人审的必须就是发出去的那份**：发布时重算的话，运营看着
A 点了发布、客户收到的是 B。快照本身的理由见 `models/report.py` 的类 docstring。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adpilot.config import Settings
from adpilot.db.postgres import transaction
from adpilot.llm import prompts
from adpilot.llm.base import LLMProvider
from adpilot.llm.contracts import ActionLine, DailyReportInput, DailyReportNarrative, MetricLine
from adpilot.models.action import Action
from adpilot.models.ad_account import AdAccount
from adpilot.models.alert import AlertStatus
from adpilot.models.report import Report, ReportStatus
from adpilot.rules import anomaly as anomaly_rules
from adpilot.services import action as action_service
from adpilot.services import ad_account as ad_account_service
from adpilot.services import alert as alert_service
from adpilot.services import daily_metric as daily_metric_service
from adpilot.services import llm as llm_service
from adpilot.services.exceptions import (
    ConflictError,
    DomainError,
    NotFoundError,
    QuotaExceededError,
)

log = structlog.get_logger(__name__)

#: 定时那条链只回头看这么多天。⚠️ 待定参数，当前取 3。
#:
#: 🔴 **不卡这个窗口会立刻踩一个坑**：运营补导一份跨 28 天的历史 CSV，归一化写了
#: 28 天，于是一口气生成 28 份日报 —— 以及 28 次 LLM 调用。那是真金白银，而且没有
#: 人会看三周前的日报。
#:
#: 3 天盖得住「周五的数据周一才导」这种常见的拖延，同时把「补历史」挡在外面。
#: 手动生成接口不受它限制 —— 那边是人指名要哪一天。
RECENT_DAYS = 3

#: 一次日报最多带几条告警摘要进提示词。开着二十条告警的账户不需要日报，需要的是
#: 有人去处理它 —— 而把它们全塞进提示词只会让那一行人话变成告警的复读。
_MAX_ALERTS = 10


async def generate(
    session: AsyncSession,
    settings: Settings,
    *,
    account_id: int,
    stat_date: date,
    provider: LLMProvider | None = None,
) -> Report:
    """生成（或重新生成）某个账户某一天的日报。

    **已发布的那份不能重新生成**（`ConflictError`）—— 客户手上那份不会自己更新，
    库里这份也就不该变。数字后来修正了，在新一期日报里说明（glossary 的「日报
    快照」）。

    还没发布的那份重新生成会**覆盖数字和 LLM 原文，并清掉人工修订**：数字变了，
    之前基于旧数字写的那段话就未必还成立，留着它比丢掉更危险。
    """
    account = await ad_account_service.get(session, account_id)
    existing = await _find(session, account_id=account_id, stat_date=stat_date)

    if existing is not None and existing.status is ReportStatus.PUBLISHED:
        raise ConflictError(
            f"{stat_date.isoformat()} 的日报已经发布，不能重新生成 —— "
            "数字后来修正了就在新一期里说明"
        )

    report = (
        existing if existing is not None else Report(account_id=account.id, stat_date=stat_date)
    )
    await _fill_facts(session, report, account=account, stat_date=stat_date)
    session.add(report)

    try:
        await session.flush()
    except IntegrityError as exc:  # 并发生成同一天：唯一键兜底
        raise ConflictError(f"{stat_date.isoformat()} 的日报正在生成") from exc

    await _write_narrative(session, settings, report, account=account, provider=provider)
    log.info(
        "report_generated",
        account_id=account_id,
        stat_date=stat_date.isoformat(),
        status=report.status.value,
        actions=len(report.actions_snapshot),
    )
    return report


@dataclass(frozen=True, slots=True)
class DueReport:
    """一份**该出但还没出**的日报。"""

    account_id: int
    account_name: str
    stat_date: date


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    """一轮定时生成的结果。"""

    #: 这一轮看了几个账户（在投 + 开着 `auto_report` 的）。
    accounts: int

    generated: int

    #: 生成失败的份数。**一个账户失败不拖累其它**，所以这个数可以大于 0 而整轮
    #: 仍然算跑完了。
    failed: int

    #: 撞上每日 LLM 额度上限而**提前中断**。为真时后面那些账户这一轮根本没看 ——
    #: 不报出来的话，「今天怎么少了几份」会变成一桩无头案。
    quota_exhausted: bool = False


async def due_reports(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[DueReport]:
    """扫出此刻**该出但还没出**的那些日报。

    判据是三个条件，缺一不可（[定时日报设计][d] 第三节）：

    1. 那天在**账户时区**下已经结束，且已过账户的 `report_delay_hours`；
    2. 那天**有 `daily_metrics` 数据** —— 没数据的那天生成出来是一份空日报，而
       空日报比没有日报更糟：它看起来像是「昨天什么都没花」；
    3. 那天**还没有任何日报**（含 draft）—— 人可能正在改那份 draft，重新生成会
       把他改好的那版清掉。

    外加两个账户级开关：`is_active`（停投的不看，同告警巡检）和 `auto_report`
    （那是省钱的闸门，见 `models/ad_account.py`）。

    🔴 **只回头看 `RECENT_DAYS` 天**，理由见那个常量 —— 不卡窗口的话，一次历史
    补导会炸出几十份日报和同样多次 LLM 调用。

    **这个函数本身不写任何东西**，所以它可以被反复调用、也可以单独测。

    [d]: ../../../docs/design/2026-08-21-scheduled-reports.md
    """
    moment = now or datetime.now(UTC)

    accounts = (
        await session.scalars(
            select(AdAccount)
            .where(AdAccount.is_active.is_(True), AdAccount.auto_report.is_(True))
            .order_by(AdAccount.id)
        )
    ).all()

    due: list[DueReport] = []
    for account in accounts:
        candidates = _closed_days(account, moment)
        if not candidates:
            continue

        # 一次问清「这几天里哪些有数据」和「哪些已经有日报」，而不是逐天两次查询：
        # 账户数 × 天数 × 2 次往返，在几十个账户上就已经能看出来了。
        with_data = set(
            await daily_metric_service.days_with_data(
                session, account_id=account.id, days=candidates
            )
        )
        already = set(
            (
                await session.scalars(
                    select(Report.stat_date).where(
                        Report.account_id == account.id,
                        Report.stat_date.in_(candidates),
                    )
                )
            ).all()
        )

        due.extend(
            DueReport(account_id=account.id, account_name=account.name, stat_date=day)
            for day in candidates
            if day in with_data and day not in already
        )

    log.info("due_reports_evaluated", accounts=len(accounts), due=len(due))
    return due


def _closed_days(account: AdAccount, now: datetime) -> list[date]:
    """这个账户最近哪几天**已经结束、且过了延迟**。最近的排最后。

    🔴 **判据是「那天的次日零点 + delay 是否已过」，不是「今天减一」。**

    差别在跨时区的时候，而且两个方向都错得很安静：洛杉矶的账户在 UTC 下的
    「昨天」可能还没过完（于是生成一份缺半天数据的日报），而上海的账户在
    UTC 下的「今天」其实早就结束了（于是日报晚出十几个小时）。

    夏令时那天不是 24 小时，所以是拿**那天的次日零点**去比，而不是拿「那天零点
    加 24 小时」—— 后者在切换日会差一个小时（同 `services/action.py` 的
    `window_bounds`）。
    """
    tz = ZoneInfo(account.timezone)
    today = now.astimezone(tz).date()
    delay = timedelta(hours=account.report_delay_hours)

    days: list[date] = []
    for offset in range(RECENT_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        # 那天在账户时区下的结束时刻 = 次日零点。datetime.combine 带 tzinfo 才是
        # 那个时区的零点；换算成 UTC 之后才谈得上和 now 比。
        closed_at = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
        if closed_at + delay <= now:
            days.append(day)
    return days


async def generate_due(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
    provider: LLMProvider | None = None,
) -> ScheduleSummary:
    """把此刻该出的日报都生成出来，全部是 **draft**。

    🔴 **绝不自动发布。** 发布的两条硬校验（人工修订过、操作记录非空）是设计文档
    第五节那条边界的最后一道人工闸门 —— 模型可能在散文里编一个百分比，没有机器
    拦得住。这一段省掉的不是「审」，是「点生成然后等十几秒」。

    ### 为什么收 `session_factory` 而不是 `session`

    **每份日报各自一个事务。** 一个账户的时区填错了（或者哪天的数据有问题），
    抛出来的异常会让整个外层事务回滚 —— 于是前面十个账户已经生成好的日报**一起
    没了**，而下一轮会重新烧一遍那十次 LLM 调用。

    这也是这个函数不待在 `sweep` 那种「一个 session 走到底」的形状里的原因：
    告警对账是纯数据库操作、便宜且可重放，日报每一份都花了钱。

    ### 额度用光就整轮中断

    撞上 `QuotaExceededError` 时**停下来**，而不是跳过这一个继续 —— 继续只会把
    剩下的调用全撞在同一堵墙上，然后日志里全是同一个错。已生成的那些留着，
    `quota_exhausted` 报出去。
    """
    moment = now or datetime.now(UTC)

    async with session_factory() as scan_session:
        due = await due_reports(scan_session, now=moment)
        accounts = len({item.account_id for item in due})

    generated = 0
    failed = 0
    quota_exhausted = False

    for item in due:
        try:
            async with transaction(session_factory) as session:
                await generate(
                    session,
                    settings,
                    account_id=item.account_id,
                    stat_date=item.stat_date,
                    provider=provider,
                )
            generated += 1
        except QuotaExceededError as exc:
            log.warning(
                "scheduled_reports_quota_exhausted",
                account_id=item.account_id,
                stat_date=item.stat_date.isoformat(),
                reason=exc.message,
                remaining=len(due) - generated - failed,
            )
            quota_exhausted = True
            break
        except DomainError as exc:
            # 这一个账户的数据有问题（时区名不存在、那天的数据在两次查询之间被
            # 删了）。记下来接着走下一个 —— 一份出不来不该拖累其余的。
            log.error(
                "scheduled_report_failed",
                account_id=item.account_id,
                stat_date=item.stat_date.isoformat(),
                reason=exc.message,
            )
            failed += 1

    summary = ScheduleSummary(
        accounts=accounts,
        generated=generated,
        failed=failed,
        quota_exhausted=quota_exhausted,
    )
    log.info(
        "scheduled_reports_finished",
        accounts=summary.accounts,
        due=len(due),
        generated=summary.generated,
        failed=summary.failed,
        quota_exhausted=summary.quota_exhausted,
    )
    return summary


async def revise(
    session: AsyncSession,
    *,
    report_id: int,
    narrative: dict[str, Any],
    reviewer: str | None = None,
) -> Report:
    """存下人工修订后的那一版。

    **不动 `llm_narrative`** —— 模型原文永不修改，那是「这句话是模型写的还是人改
    的」唯一的依据（设计文档第六节）。

    已发布的改不了：客户手上那份不会跟着更新，而库里和客户手里说的不是一回事，
    比两边都是旧的更糟。
    """
    report = await get(session, report_id)
    if report.status is ReportStatus.PUBLISHED:
        raise ConflictError("日报已经发布，不能再改 —— 要更正就在新一期里说明")

    report.narrative = narrative
    report.reviewer = reviewer
    # 🔴 盖这个戳就是「人看过并确认了」，它是发布的前置条件。判据用它而不是
    # 「narrative 有没有内容」：人工确认是一个需要显式留痕的动作，不该从内容反推。
    report.reviewed_at = datetime.now(UTC)
    report.status = ReportStatus.PENDING_REVIEW

    await session.flush()
    log.info("report_revised", report_id=report_id, reviewer=reviewer)
    return report


async def publish(session: AsyncSession, *, report_id: int) -> Report:
    """发布。**两条硬校验都在这里，不在 UI 上。**

    * **必须经过人工修订**：模型可能在散文里编一个百分比，而没有任何机器判定拦得
      住 —— 人是这件事唯一的防线（[设计][d]第三节）。
    * **操作记录不能为空**：「本期做了什么」是日报的交付价值所在，动作要可数
      （主设计文档第十节第 4 条）。

    校验的是**快照里的**操作记录，不是此刻 `actions` 表里的：日报里写的和发布时
    校验的必须是同一份，否则会出现「校验通过了，但日报正文里那段是空的」。

    [d]: ../../../docs/design/2026-08-21-llm-reports.md
    """
    report = await get(session, report_id)

    if report.status is ReportStatus.PUBLISHED:
        raise ConflictError("这份日报已经发布过了")
    if report.reviewed_at is None:
        raise ConflictError("日报必须经人工修订才能发布 —— 那是数字正确性的最后一道防线")
    if not report.actions_snapshot:
        raise ConflictError(
            "这一期没有任何操作记录，发不出去。先登记当天做过的调整，再重新生成日报"
        )

    report.status = ReportStatus.PUBLISHED
    report.published_at = datetime.now(UTC)

    await session.flush()
    log.info("report_published", report_id=report_id, account_id=report.account_id)
    return report


async def get(session: AsyncSession, report_id: int) -> Report:
    report = await session.get(Report, report_id)
    if report is None:
        raise NotFoundError(f"日报不存在：{report_id}")
    return report


async def list_page(
    session: AsyncSession,
    *,
    account_id: int,
    page: int,
    page_size: int,
) -> tuple[Sequence[Report], int]:
    """某账户的日报，最近那天的在前。"""
    await ad_account_service.get(session, account_id)

    where = Report.account_id == account_id
    total = await session.scalar(select(func.count(Report.id)).where(where))
    rows = await session.scalars(
        select(Report)
        .where(where)
        .order_by(Report.stat_date.desc(), Report.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def list_for_client(
    session: AsyncSession,
    *,
    client_id: int,
    page: int,
    page_size: int,
) -> tuple[Sequence[Report], int]:
    """客户端那条路径上的日报清单。`client_id` 必填，不给根本调不通。

    🔴 **只返回 `published`。** 这是作用域之外的**第二把锁**（设计文档第四节）：
    客户不该看到草稿，更不该看到没人审过的模型原文。条件写在这里而不是接口层，
    是因为接口层多一个分支就多一次漏掉的机会。

    不按账户筛：客户要的是「最近的日报」，而按账户筛会多出一个需要校验归属的
    入口，收益只是少几行（同 `alert.list_for_client`）。
    """
    filters = [
        AdAccount.client_id == client_id,
        Report.status == ReportStatus.PUBLISHED.value,
    ]
    joined = select(Report).join(AdAccount, Report.account_id == AdAccount.id).where(*filters)

    total = await session.scalar(
        select(func.count())
        .select_from(Report)
        .join(AdAccount, Report.account_id == AdAccount.id)
        .where(*filters)
    )
    rows = await session.scalars(
        joined.order_by(Report.stat_date.desc(), Report.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return rows.all(), total or 0


async def get_for_client(session: AsyncSession, *, client_id: int, report_id: int) -> Report:
    """客户端看一份日报。不属于自己的、或者还没发布的，一律 404。

    **没发布的也报 404 而不是 403** —— 403 等于承认「那份日报存在，只是不给你看」，
    而草稿存不存在本来就不该让客户知道（同 `api.md` 里「越权一律 404」那条）。
    """
    row = (
        await session.scalars(
            select(Report)
            .join(AdAccount, Report.account_id == AdAccount.id)
            .where(
                Report.id == report_id,
                AdAccount.client_id == client_id,
                Report.status == ReportStatus.PUBLISHED.value,
            )
        )
    ).first()

    if row is None:
        raise NotFoundError(f"日报不存在：{report_id}")
    return row


# --- 第 1 段：确定性的那部分 -------------------------------------------------


async def _fill_facts(
    session: AsyncSession,
    report: Report,
    *,
    account: AdAccount,
    stat_date: date,
) -> None:
    """把数字、操作记录、告警固定进这一行。**这一段不碰 LLM。**"""
    baseline_date = stat_date - timedelta(days=anomaly_rules.COMPARISON_LAG_DAYS)
    totals = await daily_metric_service.totals_on_days(
        session,
        account_id=account.id,
        days=[stat_date, baseline_date],
    )
    today = totals.get(stat_date)
    baseline = totals.get(baseline_date)

    actions = await action_service.list_in_window(
        session,
        account=account,
        start=stat_date,
        end=stat_date,
    )
    # 当时开着的告警。取 message 而不是整条：那句话是规则算出来的事实，直接能进
    # 日报（`alerts.message` 的 docstring），而 detail 里的数字日报自己已经有了。
    open_alerts, _ = await alert_service.list_alerts(
        session,
        status=AlertStatus.OPEN,
        account_id=account.id,
        page=1,
        page_size=_MAX_ALERTS,
    )

    report.status = ReportStatus.DRAFT
    report.currency = account.currency
    report.timezone = account.timezone

    # 那天没有任何数据时全部记 0：这一行的存在本身说明「我们看过那天了」，而
    # `days_with_data` 那套「缺数据 ≠ 没花钱」的区分在这里由**对照期是否为空**
    # 表达 —— 日报是发给客户的成品，不是可以留空的中间结果。
    report.spend = today.spend if today else Decimal(0)
    report.impressions = today.impressions if today else 0
    report.clicks = today.clicks if today else 0
    report.conversions = today.conversions if today else Decimal(0)
    report.revenue = today.revenue if today else Decimal(0)

    # 🔴 对照期缺数据就三列全空，环比整段留白 —— 不拿 0 当基线算出「上升了 100%」。
    report.baseline_date = baseline_date if baseline else None
    report.baseline_spend = baseline.spend if baseline else None
    report.baseline_conversions = baseline.conversions if baseline else None

    report.actions_snapshot = [_action_snapshot(row) for row in actions]
    report.alerts_snapshot = [row.message for row in open_alerts]
    report.generated_at = datetime.now(UTC)

    # 重新生成会清掉上一轮的人话：数字变了，基于旧数字写的那段话未必还成立。
    report.llm_narrative = None
    report.llm_call_id = None
    report.narrative = None
    report.reviewed_at = None
    report.reviewer = None


def _action_snapshot(action: Action) -> dict[str, Any]:
    """一条操作记录在日报里的副本。

    存副本而不是关联查询：删掉或改掉一条操作记录不该让**已发布**的日报内容跟着变
    （`models/report.py` 讲了为什么）。
    """
    return {
        "performed_at": action.performed_at.isoformat(),
        "kind": action.kind.value,
        "summary": action.summary,
        "reason": action.reason,
        "object_name": action.object_name,
    }


# --- 第 2 段：可能失败的那部分 -----------------------------------------------


async def _write_narrative(
    session: AsyncSession,
    settings: Settings,
    report: Report,
    *,
    account: AdAccount,
    provider: LLMProvider | None,
) -> None:
    """让模型写那一行人话。**失败不抛，日报停在 `draft`。**

    没配 LLM 也走这条路径（直接返回）：那是正常状态，不是故障 —— 陌生人 clone
    下来没有 API key，日报照样要出得来，只是人话得自己写。
    """
    if provider is None and not settings.llm_is_configured:
        log.info("report_narrative_skipped", reason="llm_not_configured", report_id=report.id)
        return

    outcome = await llm_service.run(
        session,
        settings,
        prompt=prompts.DAILY_REPORT,
        payload=_llm_input(report, account=account),
        output_type=DailyReportNarrative,
        account_id=account.id,
        provider=provider,
    )
    report.llm_call_id = outcome.call.id

    if outcome.output is None:
        # 停在 draft，人话字段留空。调用方（接口 / 后台页面）据此显示「未生成」。
        log.warning(
            "report_narrative_failed", report_id=report.id, status=outcome.call.status.value
        )
        return

    report.llm_narrative = outcome.output.model_dump()
    report.status = ReportStatus.PENDING_REVIEW


def _llm_input(report: Report, *, account: AdAccount) -> DailyReportInput:
    """把这一行翻译成模型的输入契约。

    **这一步是显式的翻译，不是「把 ORM 对象扔给模型」**（设计文档第二节）：它决定
    了模型能看到什么 —— 没有账户 ID、客户 ID、内部主键，因为那些东西会跟着提示词
    发到第三方服务器上，而它们对措辞没有任何帮助。
    """
    return DailyReportInput(
        account_name=account.name,
        stat_date=report.stat_date.isoformat(),
        timezone=report.timezone,
        currency=report.currency,
        metrics=_metric_lines(report),
        alerts=list(report.alerts_snapshot),
        actions=[
            ActionLine(
                performed_at=str(row["performed_at"]),
                summary=str(row["summary"]),
                reason=str(row["reason"]),
            )
            for row in report.actions_snapshot
        ],
    )


def _metric_lines(report: Report) -> list[MetricLine]:
    """给模型看的那几个数字，**已经算好、已经格式化**。

    模型拿到的是字符串（`"1234.5600 USD"`、`"较上周同日 +24.1%"`），不是可以再拿去
    运算的数值 —— 它的活是措辞，不是算术（`llm/contracts.py` 讲了为什么连输入侧
    也是字符串）。
    """
    cpa = anomaly_rules.cost_per_action(report.spend, report.conversions)
    baseline_cpa = (
        anomaly_rules.cost_per_action(report.baseline_spend, report.baseline_conversions)
        if report.baseline_spend is not None and report.baseline_conversions is not None
        else None
    )

    return [
        MetricLine(
            label="花费",
            value=f"{report.spend} {report.currency}",
            change=_change(report.spend, report.baseline_spend),
        ),
        MetricLine(label="展示", value=str(report.impressions)),
        MetricLine(label="点击", value=str(report.clicks)),
        MetricLine(
            label="转化数",
            value=str(report.conversions),
            change=_change(report.conversions, report.baseline_conversions),
        ),
        MetricLine(
            label="CPA",
            value=f"{cpa} {report.currency}" if cpa is not None else "无定义（当日没有转化）",
            change=_change(cpa, baseline_cpa),
        ),
    ]


def _change(current: Decimal | None, baseline: Decimal | None) -> str | None:
    """环比一句话。**算不出来就返回 `None`，让它整段留白。**

    三种算不出来：对照期没有数据、对照值是 0（除法没有意义）、当期值本身无定义
    （比如没有转化时的 CPA）。返回一个「+100%」比留白危险得多 —— 模型会照着它
    写进日报，而那个百分比是凭空的。
    """
    if current is None or baseline is None or baseline == 0:
        return None

    percent = ((current - baseline) / baseline * 100).quantize(Decimal("0.1"))
    sign = "+" if percent >= 0 else ""
    return f"较上周同日 {sign}{percent}%"


async def _find(session: AsyncSession, *, account_id: int, stat_date: date) -> Report | None:
    rows = await session.scalars(
        select(Report).where(Report.account_id == account_id, Report.stat_date == stat_date)
    )
    return rows.first()
