"""0005: 批次与库存策略。

新库由 0001 的 `create_all` 直接建出；本迁移只作用于**存量库**（离线 --sql 渲染跳过）。

Revision ID: 0005_batches
Revises: 0004_inventory_movement
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_batches"
down_revision: str | None = "0004_inventory_movement"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    from alembic import context as alembic_context

    if alembic_context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    product_columns = [column["name"] for column in inspector.get_columns("product")]
    if "cost_method" not in product_columns:
        op.add_column("product", sa.Column("cost_method", sa.String(length=8), nullable=True))

    doc_columns = [column["name"] for column in inspector.get_columns("doc_header")]
    if "cogs_fen" not in doc_columns:
        op.add_column("doc_header", sa.Column("cogs_fen", sa.BigInteger(), nullable=True))

    tables = set(inspector.get_table_names())
    if "stock_batch" not in tables:
        op.create_table(
            "stock_batch",
            sa.Column("batch_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("store_id", sa.Uuid(), nullable=False),
            sa.Column("product_id", sa.Uuid(), nullable=False),
            sa.Column("doc_line_id", sa.Uuid(), nullable=False),
            sa.Column("partner_id", sa.Uuid(), nullable=True),
            sa.Column("qty_in", sa.Numeric(18, 4), nullable=False),
            sa.Column("qty_left", sa.Numeric(18, 4), nullable=False),
            sa.Column("unit_cost_fen", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("expiry_date", sa.Date(), nullable=True),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
            sa.UniqueConstraint("tenant_id", "doc_line_id", name="uq_batch_tenant_doc_line"),
            sa.CheckConstraint("status IN ('open','exhausted','adjusted')", name="ck_batch_status"),
            sa.CheckConstraint("qty_left >= 0", name="ck_batch_qty_nonneg"),
        )
        op.create_index(
            "ix_batch_pick",
            "stock_batch",
            ["tenant_id", "store_id", "product_id", "status", "expiry_date", "received_at"],
        )
        if dialect == "postgresql":
            _rls("stock_batch")

    if "inventory_policy" not in tables:
        op.create_table(
            "inventory_policy",
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenant.tenant_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("cost_method", sa.String(length=8), nullable=False, server_default="fifo"),
            sa.Column("allow_negative", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint("cost_method IN ('fifo','fefe','avg')", name="ck_policy_cost_method"),
        )
        if dialect == "postgresql":
            _rls("inventory_policy")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "inventory_policy" in tables:
        op.drop_table("inventory_policy")
    if "stock_batch" in tables:
        op.drop_index("ix_batch_pick", table_name="stock_batch")
        op.drop_table("stock_batch")
    product_columns = [column["name"] for column in inspector.get_columns("product")]
    if "cost_method" in product_columns:
        op.drop_column("product", "cost_method")
    doc_columns = [column["name"] for column in inspector.get_columns("doc_header")]
    if "cogs_fen" in doc_columns:
        op.drop_column("doc_header", "cogs_fen")
