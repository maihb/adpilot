"""邀请码：客户端换 token 的凭证。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from adpilot.db.postgres import Base
from adpilot.models.mixins import TimestampMixin


class Invite(Base, TimestampMixin):
    """一个客户的邀请码。运营生成，渲染成二维码或链接发给客户。

    ### 为什么它要存库，而 token 不用

    区别只有一个：**要不要能撤销**。token 是自签的、自包含的，签出去就收不回来
    （[设计文档第四节](../../../docs/design/2026-08-21-client-auth.md)）；邀请码
    需要能被作废、需要能列出来看「这个客户的码什么时候发的、有没有人用过」——
    这些都是状态，所以它走正常的 `models` → `services` 分层，不进 `auth/`。

    ### 为什么不是严格的「一次性」

    主设计文档原本写的是「一次性链接/二维码」，落地时改成**有效期内可多次使用**：
    一个客户往往有老板和运营两个人要看，还可能换手机、清缓存、小程序被系统清理
    —— 每次都要运营重新生成一个，那是把成本转嫁给了唯一不该被打扰的人。

    取而代之的约束是这张表上的三样东西：`expires_at`（默认 30 天）、`revoked_at`
    （一键失效，用于「这个客户不合作了」），以及 `use_count` / `last_used_at`
    （运营唯一能自查「这个码到底发出去没有」的信号）。

    ### 🔴 存哈希，不存码本身

    库被读走的话，明文码等于一把把能直接换 token 的钥匙。哈希用 **SHA-256 而不是
    argon2**：码是 `secrets.token_urlsafe(24)`（192 位熵），慢哈希防不住任何本来
    防得住的攻击，只会让每次兑换白等几十毫秒。运营密码那边正相反，见
    `auth/password.py`。

    代价说清楚：**明文只在生成的那一次返回**，之后谁也拿不回来（要就重新生成
    一个）。运营想再看一眼上次那个二维码是做不到的。
    """

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)

    client_id: Mapped[int] = mapped_column(
        # RESTRICT 与 `ad_accounts` 一致：客户下面还挂着东西就不许删。停止合作走
        # `Client.is_active`，删除从来不是这个系统里的正常操作。
        ForeignKey("clients.id", ondelete="RESTRICT"),
        index=True,
    )

    #: SHA-256 的十六进制串，定长 64。唯一约束顺带是兑换时那次查询的索引 ——
    #: 撞了说明随机源出了问题，那种情况宁可写失败也不要静默共用一个码。
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    #: 作废时刻。`None` 表示没被作废过。**已经换出去的 token 不受影响** ——
    #: 它们是自包含的，最多再活 7 天。这一条要让运营知道，别以为点了就断了。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
