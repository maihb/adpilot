"""建两张表：商品与库存快照；并把 alerts 改成两级

D16。库存断货是 MVP 范围里最后一个没打勾的规则
（docs/design/2026-08-21-stock-alerts.md）。

**这一份手写，不是 autogenerate 的产物。** 两个原因：`alerts` 上的两个部分索引是
autogenerate 的盲区（生成的 diff 不可信，Schema 方案第四节），而 `alerts.client_id`
是加在**有数据的表**上的 NOT NULL 列 —— 那要「先可空 → 回填 → 再收紧」三步，
autogenerate 只会生成中间那步缺失的一步版本，然后在任何非空库上失败。

Revision ID: 3b7e91d4a2c8
Revises: 1ddd56e7bf13
Create Date: 2026-08-21 15:33:04.117204
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b7e91d4a2c8"
down_revision: str | Sequence[str] | None = "1ddd56e7bf13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
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
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_products_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("client_id", "sku", name=op.f("uq_products_client_id_sku")),
    )
    op.create_index(
        op.f("ix_products_client_id_is_active"),
        "products",
        ["client_id", "is_active"],
        unique=False,
    )

    op.create_table(
        "stock_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("daily_sales", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=256), nullable=True),
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
            ["product_id"],
            ["products.id"],
            name=op.f("fk_stock_snapshots_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_snapshots")),
        sa.UniqueConstraint(
            "product_id",
            "captured_at",
            name=op.f("uq_stock_snapshots_product_id_captured_at"),
        ),
    )
    op.create_index(
        op.f("ix_stock_snapshots_product_id_captured_at"),
        "stock_snapshots",
        ["product_id", "captured_at"],
        unique=False,
    )

    # ---- alerts 从「只有账户级」变成「账户级 + 客户级」----------------------
    #
    # 🔴 三步走，因为这张表可能已经有数据了。直接 `nullable=False` 建列，在任何
    # 非空库上都会当场失败（"column contains null values"），而那时人已经在生产
    # 上了。先可空、回填、再收紧是标准做法，Schema 方案第三节写着。
    op.add_column("alerts", sa.Column("client_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE alerts
           SET client_id = ad_accounts.client_id
          FROM ad_accounts
         WHERE alerts.account_id = ad_accounts.id
        """
    )
    op.alter_column("alerts", "client_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_alerts_client_id_clients"),
        "alerts",
        "clients",
        ["client_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 客户级告警（库存断货）不指向任何账户。
    op.alter_column("alerts", "account_id", existing_type=sa.Integer(), nullable=True)

    op.create_index(
        op.f("ix_alerts_client_id_status"), "alerts", ["client_id", "status"], unique=False
    )
    # 🔴 客户级那一半的去重键。**少了它不会有任何报错** —— PostgreSQL 的唯一索引
    # 里 NULL 不等于 NULL，于是既有的 uq_alerts_open_subject 对 account_id 为
    # NULL 的行形同虚设，巡检会每小时新开一条一模一样的库存告警。
    op.create_index(
        "uq_alerts_open_client_subject",
        "alerts",
        ["client_id", "kind", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'open' AND account_id IS NULL"),
    )


def downgrade() -> None:
    # 客户级告警在回滚后的 schema 里无处安放（account_id 要变回 NOT NULL）。
    # 删的是巡检随时能重新算出来的派生数据，不是原始事实 —— 库存快照本身还在
    # stock_snapshots 里，回滚再前滚一次，这些行会被下一轮巡检重新开出来。
    op.execute("DELETE FROM alerts WHERE account_id IS NULL")

    op.drop_index(
        "uq_alerts_open_client_subject",
        table_name="alerts",
        postgresql_where=sa.text("status = 'open' AND account_id IS NULL"),
    )
    op.drop_index(op.f("ix_alerts_client_id_status"), table_name="alerts")
    op.alter_column("alerts", "account_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint(op.f("fk_alerts_client_id_clients"), "alerts", type_="foreignkey")
    op.drop_column("alerts", "client_id")

    op.drop_index(op.f("ix_stock_snapshots_product_id_captured_at"), table_name="stock_snapshots")
    op.drop_table("stock_snapshots")
    op.drop_index(op.f("ix_products_client_id_is_active"), table_name="products")
    op.drop_table("products")
