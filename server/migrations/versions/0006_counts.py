"""0006: 盘点单与盘点行。

新库由 0001 的 create_all 直接建出；本迁移只作用于存量库。

Revision ID: 0006_counts
Revises: 0005_batches
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_counts"
down_revision: str | None = "0005_batches"
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
    tables = set(inspector.get_table_names())

    if "count_session" not in tables:
        op.create_table(
            "count_session",
            sa.Column("count_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("store_id", sa.Uuid(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
            sa.Column("started_by", sa.Uuid(), nullable=False),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("status IN ('open','applied','cancelled')", name="ck_count_status"),
        )
        op.create_index(
            "ix_count_query", "count_session", ["tenant_id", "store_id", "started_at"]
        )
        if dialect == "postgresql":
            _rls("count_session")

    if "count_line" not in tables:
        op.create_table(
            "count_line",
            sa.Column("count_line_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("count_id", sa.Uuid(), nullable=False),
            sa.Column("product_id", sa.Uuid(), nullable=False),
            sa.Column("qty_book", sa.Numeric(18, 4), nullable=False),
            sa.Column("qty_counted", sa.Numeric(18, 4), nullable=True),
            sa.Column("reason", sa.String(length=24), nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "count_id", "product_id", name="uq_count_line_tenant_count_product"
            ),
            sa.CheckConstraint("qty_book >= 0", name="ck_count_book_nonneg"),
            sa.CheckConstraint(
                "qty_counted IS NULL OR qty_counted >= 0", name="ck_count_counted_nonneg"
            ),
        )
        op.create_index("ix_count_line_query", "count_line", ["tenant_id", "count_id"])
        if dialect == "postgresql":
            _rls("count_line")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "count_line" in tables:
        op.drop_index("ix_count_line_query", table_name="count_line")
        op.drop_table("count_line")
    if "count_session" in tables:
        op.drop_index("ix_count_query", table_name="count_session")
        op.drop_table("count_session")
