"""告警：巡检发现的、还没解决的问题。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.mixins import TimestampMixin


class AlertKind(StrEnum):
    """告警种类。存的是 varchar，加一个成员**不需要迁移**（见 `models/types.py`）。

    ⚠️ 加成员**要改两个前端**：`KIND_NAMES` 那两份中文名映射由
    `tests/test_frontend_source.py` 双向盯着，少一个就红。
    """

    BALANCE_LOW = "balance_low"
    METRIC_ANOMALY = "metric_anomaly"

    #: 库存可撑天数低于阈值。**客户级** —— 它是这张表里第一条 `account_id` 为
    #: NULL 的告警，理由见 `Alert` 的类 docstring。
    STOCK_LOW = "stock_low"

    #: 自动拉取失败，或者太久没拉到数了。
    #:
    #: 🔴 **这一条和其它三条的性质不同**：那三条说的是「数据显示出了问题」，
    #: 这一条说的是「数据本身可能是假的」。拉取一停，看板上的花费就变成 0，
    #: 而 0 花费和「昨天没投放」在每一屏上都长得一模一样 —— 于是余额告警安静
    #: 下来、日报照常生成、所有数字看起来都很正常。见
    #: [自动拉取设计](../../../docs/design/2026-08-25-ads-api-fetch.md)第三节。
    FETCH_FAILED = "fetch_failed"


class AlertStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class Alert(Base, TimestampMixin):
    """一条告警，从「发现」到「不再成立」是同一行。

    ### 🔴 为什么是状态机，不是每轮巡检写一条

    巡检每小时跑一次，而一个余额不足的账户可能连续几天都不足。每轮写一条的话，
    三天就是七十多行**同一件事**，而「这个问题从什么时候开始的」「通知过没有」
    这两个真正要回答的问题，反而要靠翻历史去猜。

    所以：同一个 `(account_id, kind, subject)` 同时只能有一条 `open`。巡检发现它
    仍然成立就更新 `last_seen_at`，不再成立就置成 `resolved` —— 这样这张表既是
    当前待办清单（`status = open`），也是历史（全部行）。

    这条约束由**部分唯一索引**保证（`WHERE status = 'open'`），不是应用层的
    自觉：并发跑两次巡检时，靠先查再写会漏，靠数据库不会。

    ⚠️ 部分索引是 autogenerate 的盲区（[Schema 方案第四节][schema]），改动它的时候
    生成出来的 diff 不可信，必须手工确认。

    [schema]: ../../../docs/design/2026-08-19-schema-migration.md

    ### 🔴 两个去重索引，因为 NULL 不等于 NULL

    库存告警是**客户级的**（商品挂在客户上，一个客户可以有多个投放账户，推的是
    同一批货），它的 `account_id` 是 NULL。而 **PostgreSQL 的唯一索引里 NULL 不
    等于 NULL** —— 上面那个索引对 `account_id IS NULL` 的行形同虚设，两条一模
    一样的库存告警不会冲突，于是巡检每小时新开一条，一天二十四条，每条看起来都对。

    所以客户级那一半由**第二个部分索引**管（`WHERE ... AND account_id IS NULL`）。
    两个索引互不重叠：第一个只可能命中 `account_id` 非空的行（NULL 行在唯一性上
    彼此不冲突），第二个只看 NULL 行。

    详见[库存断货设计][stock]第三节。

    [stock]: ../../../docs/design/2026-08-21-stock-alerts.md

    ### `detail` 为什么是 JSONB 而不是几个列

    每种告警要记的数字不一样：余额记「还剩多少、日均多少、够几天」，指标异动记
    「哪个指标、现在多少、基线多少、变了百分之几」。摊成列的话每加一种告警就要加
    一批全是 NULL 的列。

    这是**快照，不是实时视图**：日报要能写出「触发时还剩 2.3 天」，而那时余额早就
    变了。同一个理由让日报本身也固定住当时的数字（glossary 的「日报快照」）。
    """

    __tablename__ = "alerts"

    __table_args__ = (
        # 🔴 同一件事同时只能有一条未解决。部分索引：resolved 的行不参与，所以
        # 同一个 subject 的历史可以有很多条。**只管得住 account_id 非空的行**。
        Index(
            "uq_alerts_open_subject",
            "account_id",
            "kind",
            "subject",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        # 🔴 客户级告警（account_id IS NULL）的那一半。见类 docstring 里
        # 「NULL 不等于 NULL」那一段 —— 少了这个索引不会有任何报错，只会每小时
        # 多出一条一模一样的库存告警。
        Index(
            "uq_alerts_open_client_subject",
            "client_id",
            "kind",
            "subject",
            unique=True,
            postgresql_where=text("status = 'open' AND account_id IS NULL"),
        ),
        # 待办清单的查询形态：先按状态筛，再按发生时间排。
        Index(None, "status", "opened_at"),
        # 客户端那条路径的查询形态：某个客户的告警。加了这一列之后过滤不再需要
        # JOIN 回 ad_accounts。
        Index(None, "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)

    #: 🔴 **每条告警都属于某个客户**，账户级的从账户推导得出。它是客户端作用域
    #: 过滤的落点 —— 有了它，`list_for_client` 不必 JOIN 回 `ad_accounts`，而
    #: 少一次 JOIN 就是少一个能漏掉作用域的地方（CLAUDE.md 硬规矩 4）。
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))

    #: 这条告警指向哪个投放账户。**NULL 表示它是客户级的**（目前只有库存断货：
    #: 商品挂在客户上，同一批货可能被多个账户在推）。
    account_id: Mapped[int | None] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"))

    # kind / status 用 varchar 存枚举值。这里没走 StrEnumType，是因为部分索引的
    # WHERE 子句要按字面量比较（`status = 'open'`），而那个自定义类型在索引表达式
    # 里帮不上忙、只会让迁移里的渲染多绕一层。值的合法性由服务层保证。
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))

    #: 这条告警说的是账户里的**哪件事**。余额只有一件（填 `balance`），指标异动
    #: 每个指标一件（填 `metric:spend`）。它是去重键的一部分 —— 没有它，同一个
    #: 账户的花费异动和 CPA 异动会互相顶掉。
    subject: Mapped[str] = mapped_column(String(128))

    #: 人话摘要，直接能贴进日报。**由服务层拼，不由 LLM 写** —— 这是规则算出来的
    #: 事实，不是解释（设计文档第五节的边界）。
    message: Mapped[str] = mapped_column(String(512))

    #: 触发时算出来的数字，见类 docstring。
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB())

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    #: 最近一次巡检确认它**仍然成立**的时刻。和 `updated_at` 不是一回事：那个是
    #: 「这行被改过」，这个是「问题还在」。
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 推送成功的时刻。`None` 表示还没推出去（没配 webhook，或者推失败了）。
    #: **只在告警新开时推一次** —— 每轮巡检都推的话，一个持续三天的问题会发
    #: 七十多条一模一样的消息，而人会去把通知静音。
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
