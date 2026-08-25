"""自动拉取的两张表：平台授权凭据，以及每个账户上一次拉取的结局。

设计见[自动拉取平台数据](../../../docs/design/2026-08-25-ads-api-fetch.md)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, SmallInteger, String, Text, text, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.ad_account import Platform
from adpilot.models.mixins import TimestampMixin
from adpilot.models.types import StrEnumType


class PlatformCredential(Base, TimestampMixin):
    """一次平台授权。**一行 = 一个 access_token**，覆盖它授权到的那批广告账户。

    ### 为什么凭据挂在授权上，不挂在账户上

    TikTok 一次 OAuth 授权返回的是一**批** `advertiser_ids`。做成账户级的话，
    同一个 token 会被复制 N 份，撤销和轮换时必然漏掉一份 —— 而漏掉的那份不会
    报错，只会在某天突然拉不到数。

    所以是 `platform_credentials 1 —— N ad_accounts`，账户那边的
    `credential_id` **可空**：CSV 导入的账户永远没有凭据，而「没接 API」不是
    残缺状态，是这个系统一直支持的正常形态。**这也是自动拉取的开关** —— 不挂
    凭据就不会被排期扫到，不需要再加一个 `auto_fetch` 布尔。

    ### 🔴 token 密文存这里，密钥在 env

    `access_token` 列里是密文，加解密走 `auth/crypto.py`（那个模块的 docstring
    讲了为什么这件事值得做、以及它**不**防什么）。`CREDENTIALS_SECRET` 丢了，
    这一列的每一行都作废，只能重走授权。
    """

    __tablename__ = "platform_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: 业务意义上的平台，与 `ad_accounts.platform` 同一个枚举 —— 挂账户时要比对
    #: 它们一致（把 Meta 账户挂到 TikTok 的凭据上，症状是每次拉取都 401，而
    #: 报错完全不提「你挂错了」）。
    platform: Mapped[Platform] = mapped_column(StrEnumType(Platform, 16))

    #: 用哪个适配器去拉。**和 `platform` 不是同一件事**：同一个平台可以有多个
    #: 适配器（生产的 `tiktok_api`、非生产的 `fake_api`，将来可能还有走别的
    #: 端点的），而这个值决定了拿到的 payload 是什么形状 —— 它会跟着快照一起
    #: 落进 `raw_reports.provider`。
    provider: Mapped[str] = mapped_column(String(32))

    #: 人给的名字（「nail 的 BC 授权」）。后台列表全靠它认，因为 token 本身
    #: 不可读、而 `platform` 只有两三种值。
    label: Mapped[str] = mapped_column(String(128))

    #: 🔴 密文，不是明文。列名不叫 `access_token_encrypted` 是因为它只可能是
    #: 密文 —— 留一个「看起来像是能存明文」的名字，早晚有人真往里存明文。
    #: 用 Text 不是 varchar(n)：密文长度取决于明文长度和算法，钉一个上限没有
    #: 依据，而超长的后果是写入报错。
    access_token: Mapped[str] = mapped_column(Text())

    #: 授权换回来的 scope 原文，纯审计用。「为什么拉不到余额」这类问题的第一
    #: 现场就是它 —— 申请时勾漏了一个权限，表现是某个接口一直 40100。
    scope: Mapped[str | None] = mapped_column(String(512))

    #: 授权覆盖到的平台账户 ID 列表。用来在后台提示「这次授权能挂哪些账户」，
    #: **不作为权限判据** —— 真正的判据是每次调用时平台自己的回答。存它是为了
    #: 让人挂账户时不必去平台后台对着抄。
    external_account_ids: Mapped[list[str]] = mapped_column(
        JSONB(), server_default=text("'[]'::jsonb")
    )

    #: 什么时候过期。**TikTok 的长期 token 没有这个概念，所以可空** —— 留着是
    #: 给 Meta 的（那边的 token 有 60 天大限）。真接 Meta 时才写刷新逻辑，现在
    #: 写等于写一段永远不执行的代码。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 停用一个凭据（客户结束合作、token 被平台撤销）。**不删行**：删掉之后
    #: 那些历史快照的来源就成了无头案，而 `ad_accounts.credential_id` 上的
    #: 外键是 RESTRICT，删之前还得先解绑每个账户。
    is_active: Mapped[bool] = mapped_column(Boolean(), server_default=true())


class FetchState(Base, TimestampMixin):
    """某个账户上一次自动拉取的结局。**一账户一行**，每次拉取 upsert。

    ### 🔴 这张表是「拉不到数」唯一看得见的地方

    自动化把「导数据的人知道自己导了没有」这个隐含的好处拿走了。而拉取失败在
    看板上的长相是——

        昨天花费 $0，曝光 0，点击 0

    ——**和「昨天没投放」一模一样**。日报会照常生成，余额告警会因为没有新消耗而
    安静下来。整套系统会非常自信地给出一个基于空气的结论。

    所以每次拉取的结局都要落在**库里**而不是只写日志：日志没人看，而这件事必须
    能被查询、能被巡检扫到、能开告警。设计文档第三节。

    ### 为什么不塞进 `ad_accounts`

    那张表是**配置**（这个账户是谁的、什么币种、要不要日报），这张是**运行态**
    （上次什么时候成功的、连着失败几次了）。混在一起的第一个代价是每次拉取都要
    去 UPDATE 配置表，第二个代价是「改配置」和「跑任务」抢同一行的锁。

    ### `consecutive_failures` 不是为了统计

    它是告警降噪的依据：一次失败可能是平台抖了一下，连着几次才说明真的坏了。
    存计数而不是每次去数历史，是因为这张表只保留最新态 —— 历史在
    `raw_reports`（成功的那些）和告警表（失败的那些）里。
    """

    __tablename__ = "fetch_states"

    __table_args__ = (
        # 巡检要扫的是「太久没成功过的账户」，走的正是这个列。
        Index(None, "last_success_at"),
    )

    #: 主键就是账户 ID —— 一账户一行，天然唯一。单独再给一个自增主键只会多一条
    #: 需要维护的唯一约束。
    account_id: Mapped[int] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="CASCADE"), primary_key=True
    )

    #: 最后一次**尝试**的时刻（不论成败）。和下面那个分开，是因为「一直在试但
    #: 一直失败」和「根本没在试」是两种完全不同的故障，而只存一个时间戳的话，
    #: 这两者看起来一模一样。
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 最后一次**成功**的时刻。这是「数据新不新」唯一可信的依据。
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: 最后一次失败的原因，给人看的。成功之后置空 —— 留着一条已经修好的报错，
    #: 只会让下一个来看的人以为现在还坏着。
    last_error: Mapped[str | None] = mapped_column(String(512))

    #: 连续失败次数，成功归零。SmallInt 够了：真到几百次的时候，问题不在计数上。
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger(), server_default=text("0"))
