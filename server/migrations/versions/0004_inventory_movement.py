"""0004: 单据头加往来对象 + 库存流水表。

注意：`0001` 用 `Base.metadata.create_all` 建表，**新库会直接按最新模型建出这两样**。
所以本迁移对「新库」是空操作，只在**存量库**上补列/建表 + RLS，避免重复建列报错。

Revision ID: 0004_inventory_movement
Revises: 0003_fix_rls_empty_guc
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_inventory_movement"
down_revision: str | None = "0003_fix_rls_empty_guc"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 离线（--sql）渲染时不要 inspect：0001 的 create_all 已经反映最新模型，
    # 新库的 DDL 快照里已包含本次新增的列和表。条件执行只对在线库有意义。
    from alembic import context as alembic_context

    if alembic_context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    columns = [column["name"] for column in inspector.get_columns("doc_header")]
    if "partner_id" not in columns:
        op.add_column("doc_header", sa.Column("partner_id", sa.Uuid(), nullable=True))
        op.create_index(
            "ix_doc_header_partner", "doc_header", ["tenant_id", "partner_id", "biz_date"]
        )

    if "inventory_movement" in inspector.get_table_names():
        return

    op.create_table(
        "inventory_movement",
        sa.Column("move_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("doc_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=24), nullable=False),
        sa.Column("qty_base", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_cost_fen", sa.BigInteger(), nullable=True),
        sa.Column("partner_id", sa.Uuid(), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('purchase','sale','count_adjust','loss','transfer_in','transfer_out','opening')",
            name="ck_move_reason",
        ),
        sa.CheckConstraint("qty_base <> 0", name="ck_move_qty_nonzero"),
    )
    op.create_index(
        "ix_move_query",
        "inventory_movement",
        ["tenant_id", "store_id", "product_id", "occurred_at"],
    )
    op.create_index("ix_move_doc", "inventory_movement", ["tenant_id", "doc_id"])

    if dialect == "postgresql":
        op.execute("ALTER TABLE inventory_movement ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE inventory_movement FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY inventory_movement_tenant_isolation ON inventory_movement "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "inventory_movement" in inspector.get_table_names():
        op.drop_index("ix_move_doc", table_name="inventory_movement")
        op.drop_index("ix_move_query", table_name="inventory_movement")
        op.drop_table("inventory_movement")
    columns = [column["name"] for column in inspector.get_columns("doc_header")]
    if "partner_id" in columns:
        op.drop_index("ix_doc_header_partner", table_name="doc_header")
        op.drop_column("doc_header", "partner_id")
