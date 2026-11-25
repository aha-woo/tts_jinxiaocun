"""0007: M0 账本重构与 Schema 扩展（负库存 / 成本可空 / 批次标签与虚拟批 / 成本调整 / 统一待处理 / 附件）。

离线（``--sql``）模式下 ``0001_initial`` 已按最新 ``Base.metadata`` 渲染全量 DDL，本迁移跳过；
在线（存量库升级）模式下显式删掉非负 CHECK 约束、补齐新列与 3 张新表，并在 PG 上补齐 RLS。

Revision ID: 0007_m0_ledger_redesign
Revises: 0006_counts
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_m0_ledger_redesign"
down_revision: str | None = "0006_counts"
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


def _cols(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table_name)}


def _add_col_if_missing(inspector: sa.Inspector, table: str, col: sa.Column[object]) -> None:
    if col.name not in _cols(inspector, table):
        op.add_column(table, col)


def _drop_check_if_exists(inspector: sa.Inspector, table: str, name: str) -> None:
    existing = {c.get("name") for c in inspector.get_check_constraints(table)}
    if name in existing:
        op.drop_constraint(name, table, type_="check")


def upgrade() -> None:
    from alembic import context as alembic_context

    if alembic_context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name
    tables = set(inspector.get_table_names())
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    # 1. tenant / member
    _add_col_if_missing(
        inspector,
        "tenant",
        sa.Column("allow_negative", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    _add_col_if_missing(
        inspector,
        "tenant",
        sa.Column("accounting_mode", sa.String(length=16), nullable=False, server_default="per_txn"),
    )
    _add_col_if_missing(
        inspector,
        "tenant",
        sa.Column("staff_can_view_cost", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    _add_col_if_missing(
        inspector,
        "member",
        sa.Column("novice_voice_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_col_if_missing(
        inspector,
        "member",
        sa.Column("novice_first_pass_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_col_if_missing(
        inspector,
        "member",
        sa.Column("auto_post_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # 2. product
    _add_col_if_missing(inspector, "product", sa.Column("avg_cost_fen", sa.BigInteger(), nullable=True))
    _add_col_if_missing(
        inspector,
        "product",
        sa.Column("track_batch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _add_col_if_missing(inspector, "product", sa.Column("allow_negative", sa.Boolean(), nullable=True))
    _add_col_if_missing(
        inspector, "product", sa.Column("low_stock_qty", sa.Numeric(18, 4), nullable=True)
    )
    _add_col_if_missing(
        inspector,
        "product",
        sa.Column("expiry_warn_days", sa.Integer(), nullable=False, server_default="2"),
    )
    _add_col_if_missing(inspector, "product", sa.Column("category", sa.String(length=64), nullable=True))
    _add_col_if_missing(inspector, "product", sa.Column("barcode", sa.String(length=64), nullable=True))
    _add_col_if_missing(inspector, "product", sa.Column("merged_into", sa.Uuid(), nullable=True))

    if dialect == "postgresql":
        op.alter_column("product", "last_purchase_price_fen", existing_type=sa.BigInteger(), nullable=True)
        op.alter_column("product", "last_sale_price_fen", existing_type=sa.BigInteger(), nullable=True)
        _drop_check_if_exists(inspector, "product", "ck_product_status")
        op.create_check_constraint(
            "ck_product_status", "product", "status IN ('draft','active','archived','merged','disabled')"
        )
        _drop_check_if_exists(inspector, "product", "ck_product_price_nonneg")
        op.create_check_constraint(
            "ck_product_price_nonneg",
            "product",
            "(last_purchase_price_fen IS NULL OR last_purchase_price_fen >= 0) "
            "AND (last_sale_price_fen IS NULL OR last_sale_price_fen >= 0)",
        )

    # 3. doc_header / doc_line
    _add_col_if_missing(inspector, "doc_header", sa.Column("corrects_doc_id", sa.Uuid(), nullable=True))
    _add_col_if_missing(
        inspector,
        "doc_header",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="voice"),
    )
    _add_col_if_missing(inspector, "doc_line", sa.Column("batch_id", sa.Uuid(), nullable=True))
    _add_col_if_missing(inspector, "doc_line", sa.Column("price_source", sa.String(length=16), nullable=True))
    _add_col_if_missing(inspector, "doc_line", sa.Column("cost_snapshot_fen", sa.BigInteger(), nullable=True))
    _add_col_if_missing(
        inspector,
        "doc_line",
        sa.Column("cost_status", sa.String(length=16), nullable=False, server_default="final"),
    )
    if dialect == "postgresql":
        op.alter_column("doc_line", "unit_price_fen", existing_type=sa.BigInteger(), nullable=True)
        op.alter_column("doc_line", "amount_fen", existing_type=sa.BigInteger(), nullable=True)
        _drop_check_if_exists(inspector, "doc_line", "ck_doc_line_amount_nonneg")
        op.create_check_constraint(
            "ck_doc_line_amount_nonneg",
            "doc_line",
            "(amount_fen IS NULL OR amount_fen >= 0) AND (unit_price_fen IS NULL OR unit_price_fen >= 0)",
        )

    # 4. inventory / movement / stock_batch / count_session / count_line / policy
    _add_col_if_missing(inspector, "inventory", sa.Column("avg_cost_fen", sa.BigInteger(), nullable=True))
    _add_col_if_missing(
        inspector,
        "inventory",
        sa.Column("negative_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _add_col_if_missing(
        inspector, "inventory_movement", sa.Column("doc_line_id", sa.Uuid(), nullable=True)
    )
    _add_col_if_missing(
        inspector,
        "inventory_movement",
        sa.Column("cost_status", sa.String(length=16), nullable=False, server_default="final"),
    )
    _add_col_if_missing(
        inspector,
        "stock_batch",
        sa.Column("label", sa.String(length=64), nullable=False, server_default=""),
    )
    _add_col_if_missing(
        inspector,
        "stock_batch",
        sa.Column("is_virtual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _add_col_if_missing(inspector, "count_session", sa.Column("doc_id", sa.Uuid(), nullable=True))

    if dialect == "postgresql":
        _drop_check_if_exists(inspector, "inventory", "ck_inventory_qty_nonneg")
        _drop_check_if_exists(inspector, "count_line", "ck_count_book_nonneg")
        _drop_check_if_exists(inspector, "inventory_movement", "ck_move_reason")
        op.create_check_constraint(
            "ck_move_reason",
            "inventory_movement",
            "reason IN ('purchase','sale','sales_return','purchase_return','count_adjust','loss','transfer_in','transfer_out','opening','reversal','oversold_settle')",
        )
        op.alter_column("stock_batch", "doc_line_id", existing_type=sa.Uuid(), nullable=True)
        op.alter_column("stock_batch", "unit_cost_fen", existing_type=sa.BigInteger(), nullable=True)
        _drop_check_if_exists(inspector, "stock_batch", "ck_batch_qty_nonneg")
        op.create_check_constraint(
            "ck_batch_qty_nonneg", "stock_batch", "is_virtual OR qty_left >= 0"
        )
        if "inventory_policy" in tables:
            _drop_check_if_exists(inspector, "inventory_policy", "ck_policy_cost_method")
            op.create_check_constraint(
                "ck_policy_cost_method",
                "inventory_policy",
                "cost_method IN ('avg','batch','fifo','fefe','fefo')",
            )

    # 5. 新建 3 张表：cost_adjustment / gap / attachment
    if "cost_adjustment" not in tables:
        op.create_table(
            "cost_adjustment",
            sa.Column("adj_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("store_id", sa.Uuid(), nullable=False),
            sa.Column("product_id", sa.Uuid(), nullable=False),
            sa.Column("sale_doc_id", sa.Uuid(), nullable=True),
            sa.Column("sale_line_id", sa.Uuid(), nullable=True),
            sa.Column("source_doc_id", sa.Uuid(), nullable=True),
            sa.Column("qty_base", sa.Numeric(18, 4), nullable=False),
            sa.Column("old_unit_cost_fen", sa.BigInteger(), nullable=True),
            sa.Column("new_unit_cost_fen", sa.BigInteger(), nullable=False),
            sa.Column("delta_cogs_fen", sa.BigInteger(), nullable=False),
            sa.Column("reason", sa.String(length=24), nullable=False, server_default="oversold_settle"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("reason IN ('oversold_settle','backfill')", name="ck_cost_adj_reason"),
        )
        op.create_index(
            "ix_cost_adj_tenant_product",
            "cost_adjustment",
            ["tenant_id", "store_id", "product_id", "created_at"],
        )
        if dialect == "postgresql":
            _rls("cost_adjustment")

    if "gap" not in tables:
        op.create_table(
            "gap",
            sa.Column("gap_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("store_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("ref_entity", sa.String(length=32), nullable=False),
            sa.Column("ref_id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("detail_json", json_type, nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
            sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "kind IN ('missing_cost','missing_amount','missing_partner','negative_stock','unconfirmed','dup_suspect','unit_change','expiring_batch')",
                name="ck_gap_kind",
            ),
            sa.CheckConstraint("status IN ('open','snoozed','resolved')", name="ck_gap_status"),
        )
        op.create_index(
            "ix_gap_open",
            "gap",
            ["tenant_id", "store_id", "status", "priority", "created_at"],
        )
        if dialect == "postgresql":
            _rls("gap")

    if "attachment" not in tables:
        op.create_table(
            "attachment",
            sa.Column("attachment_id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("store_id", sa.Uuid(), nullable=False),
            sa.Column("doc_id", sa.Uuid(), nullable=True),
            sa.Column("kind", sa.String(length=24), nullable=False, server_default="delivery_note"),
            sa.Column("object_ref", sa.Text(), nullable=False),
            sa.Column("ocr_json", json_type, nullable=True),
            sa.Column("created_by", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_attachment_doc", "attachment", ["tenant_id", "doc_id"])
        if dialect == "postgresql":
            _rls("attachment")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for name in ("attachment", "gap", "cost_adjustment"):
        if name in tables:
            op.drop_table(name)
