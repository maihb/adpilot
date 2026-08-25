"""平台凭据与拉取状态：自动拉取的两张表 + 账户挂凭据

D19。设计见 docs/design/2026-08-25-ads-api-fetch.md。

三处改动：

* `platform_credentials` —— 一次平台授权一行，`access_token` 存的是**密文**
  （密钥在 env 的 `CREDENTIALS_SECRET`，理由见 src/adpilot/auth/crypto.py）。
* `fetch_states` —— 一个账户一行，记上次拉取的结局。主键就是 `account_id`。
  这张表是「拉不到数」唯一看得见的地方：拉取一停，看板上的 0 花费和「昨天没
  投放」长得一模一样，而告警巡检正是靠这张表把两者分开。
* `ad_accounts.credential_id` —— **可空**，因为「没接 API」是这个系统一直支持
  的正常形态（CSV 导入的账户永远没有凭据）。它同时是自动拉取的开关，所以刻意
  不再加一个 `auto_fetch` 布尔。

外键给的是 RESTRICT 而不是 CASCADE：还有账户挂着的凭据不许删 —— 同
`ad_accounts.client_id` 那条，删除从来不是这个系统里的正常操作，停用走
`is_active`。

Revision ID: 141e83b90f11
Revises: 5f2a7c9d1e83
Create Date: 2026-08-25 14:31:49.872496
"""

# 🔴 autogenerate 出来的是**草稿**，提交前人看一遍：改名会被生成成 drop + add
# （数据静默丢失），部分索引的 diff 也不可信。见 docs/design/2026-08-19-schema-migration.md

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "141e83b90f11"
down_revision: str | Sequence[str] | None = "5f2a7c9d1e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=512), nullable=True),
        sa.Column(
            "external_account_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_credentials")),
    )
    op.create_table(
        "fetch_states",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column(
            "consecutive_failures", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["ad_accounts.id"],
            name=op.f("fk_fetch_states_account_id_ad_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_fetch_states")),
    )
    op.create_index(
        op.f("ix_fetch_states_last_success_at"), "fetch_states", ["last_success_at"], unique=False
    )
    op.add_column("ad_accounts", sa.Column("credential_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_ad_accounts_credential_id"), "ad_accounts", ["credential_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_ad_accounts_credential_id_platform_credentials"),
        "ad_accounts",
        "platform_credentials",
        ["credential_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # DESTRUCTIVE-OK 不适用：这里的 drop 是回滚建表，每个建表迁移都有
    # （tests/test_migration_safety.py 只扫 upgrade）。真正会丢的是已经授权好的
    # 凭据 —— 回滚后要重走一遍每个平台的授权流程，token 换不回来。
    op.drop_constraint(
        op.f("fk_ad_accounts_credential_id_platform_credentials"), "ad_accounts", type_="foreignkey"
    )
    op.drop_index(op.f("ix_ad_accounts_credential_id"), table_name="ad_accounts")
    op.drop_column("ad_accounts", "credential_id")
    op.drop_index(op.f("ix_fetch_states_last_success_at"), table_name="fetch_states")
    op.drop_table("fetch_states")
    op.drop_table("platform_credentials")
