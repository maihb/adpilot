"""给广告账户加两个日报开关

D18。定时日报要知道两件事：这个账户要不要自动出日报（那是**省钱的闸门** ——
不看日报的账户没必要每天烧一次 LLM 调用），以及日切之后等几小时再生成。
理由见 docs/design/2026-08-21-scheduled-reports.md 第四、五节。

两列都带 server_default，所以**已有的行不需要回填** —— 这也是它们能一步建成
NOT NULL 的原因（对比 D16 给 alerts 加 client_id 那次，那列没有合理的默认值，
只能先可空、回填、再收紧）。

Revision ID: 5f2a7c9d1e83
Revises: 3b7e91d4a2c8
Create Date: 2026-08-21 17:12:41.882013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f2a7c9d1e83"
down_revision: str | Sequence[str] | None = "3b7e91d4a2c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ad_accounts",
        sa.Column("auto_report", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "ad_accounts",
        sa.Column(
            "report_delay_hours",
            sa.SmallInteger(),
            server_default=sa.text("2"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # DESTRUCTIVE-OK: 这两列是**配置**不是事实数据 —— 回滚后再前滚，它们会回到
    # 默认值（自动出日报、延迟 2 小时），而不是丢掉任何一份日报或一行指标。
    # 运营改过的开关会丢，那是回滚配置列的固有代价。
    op.drop_column("ad_accounts", "report_delay_hours")
    op.drop_column("ad_accounts", "auto_report")
