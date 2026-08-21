"""日报：发给客户的那一份。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.daily_metric import MONEY
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType


class ReportStatus(StrEnum):
    """日报的生命周期。三个状态，转换只有三条边。

    ```
    draft ──LLM 写完──▶ pending_review ──发布──▶ published ──▶ 客户可见
      ▲                      ▲
      └ LLM 失败时停在这里    └ 人工修订之后一定到这里（无论从哪来）
    ```
    """

    #: 数字已经固定，**人话还没有** —— LLM 失败或还没跑。人要自己写。
    DRAFT = "draft"

    #: 人话有了（LLM 写的或人写的），等人确认发布。
    PENDING_REVIEW = "pending_review"

    #: 已发布。**客户端只看得到这个状态的日报**，而且它从此不再变。
    PUBLISHED = "published"


class Report(Base, TimestampMixin):
    """某个账户某一天的日报。

    ### 🔴 它是快照，不是视图

    **日报一旦生成就固定住当时的数字，不随后续回填变化**（[glossary][g] 的「日报
    快照」）。所以这张表存的是一份份**摊平的数字**，而不是一个每次打开都重新
    `select` 一遍的查询。

    反过来做的后果不是「数字更准」，是**客户上周收到的日报今天再打开数字变了**
    —— 而「报表数字会自己改」是解释不清的，它会连带摧毁对全部数字的信任。数字
    后来确实修正了且值得说明时，**在新一期日报里说明，不动老的那份**。

    ⚠️ 固定发生在**生成**那一刻，不是发布那一刻。理由是人审的必须就是发出去的：
    发布时重算的话，运营看着 A 点了发布、客户收到的是 B。

    [g]: ../../../docs/business/glossary.md

    ### 🔴 两版都存，而且**LLM 那版永不修改**

    `llm_narrative` 是模型原文，`narrative` 是人改完的。要能回答「这句话是模型写
    的还是人改的」—— 那是评估这套系统值不值得信的唯一依据（设计文档第六节）。
    覆盖掉原文就等于把这个问题永久删除了。

    客户看到的是 `narrative`。而 `narrative` 为空的日报**发不出去**（服务层硬校验，
    不是 UI 提示）：那道人工闸门是「模型在散文里编了个百分比」唯一的防线。

    ### 数字为什么摊成列，而告警和操作记录是 JSONB

    和 `alerts.detail` 那个决定正好相反，理由也正好相反：告警**每一种要记的数字
    都不一样**（余额记天数、异动记百分比），摊成列就是每加一种告警加一批全是 NULL
    的列；日报的数字形状**是固定的**（就那五个指标 + 一个对照期），摊成列换来的是
    能查、能排序、能在数据库里直接对账。

    `actions_snapshot` / `alerts_snapshot` 用 JSONB，是因为它们是**当时那几条文字
    的副本**，不参与任何计算 —— 存成关联表的话，删掉一条操作记录就会让已发布的
    日报内容跟着变，而那正是快照要防的事。

    ### 派生指标一个都不存

    CPA / CTR / CPM / ROAS 全部由出参现算（[glossary][g] 的公式）。存下来等于把
    公式复制进数据库，而「同一个 CPA 出现两个值」会让整套输出的可信度归零 ——
    同 `daily_metrics` 那条。
    """

    __tablename__ = "reports"

    __table_args__ = (
        # 一个账户一天**只有一份**日报。重述不是再发一份同一天的，而是在新一期里
        # 说明（glossary）—— 客户手上同一天出现两份日报，比数字错了更让人不安。
        UniqueConstraint("account_id", "stat_date"),
        # 两个查询形态：运营看「这个账户的日报」，客户端看「已发布的那些」。
        Index(None, "account_id", "status", "stat_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)

    account_id: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"))

    #: 报告的是**哪一天**。账户时区下的自然日，口径同 `daily_metrics.stat_date`。
    stat_date: Mapped[date] = mapped_column(Date())

    status: Mapped[ReportStatus] = mapped_column(StrEnumType(ReportStatus, 16))

    # 口径在行上存一份，不 JOIN 回 ad_accounts 取：账户的币种和时区都可以被改，
    # 改了之后**已发布的日报仍要按当时的口径解释**。同 `daily_metrics.currency`，
    # 但在这里更要紧 —— 日报是已经发出去的东西。
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(64))

    # --- 当日数字快照 ---------------------------------------------------------
    spend: Mapped[Decimal] = mapped_column(MONEY)
    impressions: Mapped[int] = mapped_column(BigInteger())
    clicks: Mapped[int] = mapped_column(BigInteger())
    conversions: Mapped[Decimal] = mapped_column(MONEY)
    revenue: Mapped[Decimal] = mapped_column(MONEY)

    # --- 对照期 ---------------------------------------------------------------
    #
    # 只存花费和转化两项，不是五项全存：环比要说的是「花得多了没有、成本涨了没有」，
    # 而那两句话正好由这两个数算出来（CPA = spend / conversions）。这也是异动规则
    # 关注的同一对指标（`rules/anomaly.py` 的 AnomalyMetric），两边口径一致才对得上。
    #
    # 🔴 **三列同时为空是正常状态**：对照那天没有数据。此时环比整段留白，而不是
    # 拿 0 当基线算出一个「上升了 100%」——那是凭空的百分比（同 `totals_on_days`
    # 不补零的理由）。

    #: 对照的是哪一天。默认上周同日 —— 抵消星期几效应（glossary 的「周同比」）。
    baseline_date: Mapped[date | None] = mapped_column(Date())
    baseline_spend: Mapped[Decimal | None] = mapped_column(MONEY)
    baseline_conversions: Mapped[Decimal | None] = mapped_column(MONEY)

    # --- 文字快照 -------------------------------------------------------------

    #: 当期做了什么，`actions` 表在生成那一刻的副本（含 `reason`）。
    #: **空着的日报发不出去** —— 设计文档第十节第 4 条那条硬校验。
    actions_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())

    #: 当时开着的告警摘要（规则算出来的人话，不是 LLM 写的）。
    alerts_snapshot: Mapped[list[str]] = mapped_column(JSONB())

    # --- 两版人话 -------------------------------------------------------------

    #: 🔴 LLM 原文，**永不修改**。`None` = 没生成（模型挂了或没配 LLM）。
    #: 形状是 `llm/contracts.py` 的 `DailyReportNarrative`。
    llm_narrative: Mapped[dict[str, Any] | None] = mapped_column(JSONB())

    #: 人工修订后的版本，**客户看的是这个**。`None` = 还没人改过，发不出去。
    narrative: Mapped[dict[str, Any] | None] = mapped_column(JSONB())

    # 哪次调用生成的。SET NULL 而不是级联：`llm_calls` 是财务留痕，两者的生命周期
    # 本来就不该绑在一起。可空 —— 没配 LLM 时压根没有调用。
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id", ondelete="SET NULL"))

    # --- 时间线 ---------------------------------------------------------------

    #: 数字是**什么时候**固定下来的。日报里要写明它，客户才知道这份数据的截止点。
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    #: 人工修订的时刻。🔴 **它非空是发布的前置条件** —— 判据用它而不是「narrative
    #: 有没有内容」，是因为「人看过并确认」是一个需要显式留痕的动作，不该从内容
    #: 反推。
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 谁改的。可空，理由同 `actions.operator`（运营账号只有一个）。
    reviewer: Mapped[str | None] = mapped_column(String(64))

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
