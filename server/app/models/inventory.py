"""库存（**门店级**）：qty_base 折算到商品基本单位；version 用于影响预演 CAS。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, StoreScopedMixin, TenantMixin, created_at_column, uuid_pk


class Inventory(Base, TenantMixin, StoreScopedMixin):
    """门店 × 商品 的库存行（允许负库存，流水才是唯一数量真相）。"""

    __tablename__ = "inventory"

    inventory_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 折算到 base_unit 的数量（不用 float 存数量；允许负值）
    qty_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    #: 门店级移动加权平均成本（分，NULL=暂无）
    avg_cost_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 负库存标记（qty_base < 0 时为 True，进待处理与首页提醒）
    negative_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: CAS 乐观锁：落账时校验 preview_version，冲突 -> E_PREVIEW_VERSION_CONFLICT
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "product_id", name="uq_inventory_tenant_id_store_id_product_id"),
        Index("ix_inventory_tenant_id_store_id_product_id", "tenant_id", "store_id", "product_id"),
    )


class InventoryMovement(Base, TenantMixin, StoreScopedMixin):
    """库存流水：一切数量变动的账本。"""

    __tablename__ = "inventory_movement"

    move_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 有批次时带批次；移动平均/盘点汇总时可为空
    batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: 来源单据；盘点调整/报损可以为空
    doc_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    doc_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    #: 正=入库，负=出库（以商品基本单位计）
    qty_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    #: 基本单位成本（分，可空=未知成本）
    unit_cost_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 成本状态：final（确定）| provisional（负库存暂估）| unknown（未知）
    cost_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="final")
    partner_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_move_query", "tenant_id", "store_id", "product_id", "occurred_at"),
        Index("ix_move_doc", "tenant_id", "doc_id"),
        CheckConstraint(
            "reason IN ('purchase','sale','sales_return','purchase_return','count_adjust','loss','transfer_in','transfer_out','opening','reversal','oversold_settle')",
            name="ck_move_reason",
        ),
        CheckConstraint("cost_status IN ('final','provisional','unknown')", name="ck_move_cost_status"),
        CheckConstraint("qty_base <> 0", name="ck_move_qty_nonzero"),
    )


class StockBatch(Base, TenantMixin, StoreScopedMixin):
    """入库批次：一次进货的一行 = 一批，或虚拟「未到货」批次用于负库存追踪。"""

    __tablename__ = "stock_batch"

    batch_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    doc_line_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    partner_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: 自动生成批次名（如 "11/25 王老板" 或 "未到货"）
    label: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    #: 是否负库存「未到货」虚拟批次（可留负数余量并在补进货时抵扣）
    is_virtual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    qty_in: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_left: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    #: 基本单位成本（分，NULL=进价待补）
    unit_cost_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")

    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_line_id", name="uq_batch_tenant_doc_line"),
        Index("ix_batch_pick", "tenant_id", "store_id", "product_id", "status", "expiry_date", "received_at"),
        CheckConstraint("status IN ('open','exhausted','adjusted')", name="ck_batch_status"),
        CheckConstraint("is_virtual OR qty_left >= 0", name="ck_batch_qty_nonneg"),
    )


class InventoryPolicy(Base):
    """租户库存策略：成本法默认值、是否允许负库存（与 tenant 字段同步）。"""

    __tablename__ = "inventory_policy"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.tenant_id", ondelete="CASCADE"), primary_key=True
    )
    cost_method: Mapped[str] = mapped_column(String(8), nullable=False, server_default="avg")
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "cost_method IN ('avg','batch','fifo','fefe','fefo')",
            name="ck_policy_cost_method",
        ),
    )


class CountSession(Base, TenantMixin, StoreScopedMixin):
    """盘点单：一次盘点 = 一个会话 + 每件货一行。"""

    __tablename__ = "count_session"

    count_id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    doc_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    started_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('open','applied','cancelled')", name="ck_count_status"),
        Index("ix_count_query", "tenant_id", "store_id", "started_at"),
    )


class CountLine(Base, TenantMixin):
    """盘点行：账面数是打开盘点时的快照（允许负库存），实盘数由用户填。"""

    __tablename__ = "count_line"

    count_line_id: Mapped[uuid.UUID] = uuid_pk()
    count_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    qty_book: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_counted: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "count_id", "product_id", name="uq_count_line_tenant_count_product"),
        Index("ix_count_line_query", "tenant_id", "count_id"),
        CheckConstraint("qty_counted IS NULL OR qty_counted >= 0", name="ck_count_counted_nonneg"),
    )


class CostAdjustment(Base, TenantMixin, StoreScopedMixin):
    """成本调整记录：补进货结清负库存超卖或事后补进价时的 COGS 差额追踪。"""

    __tablename__ = "cost_adjustment"

    adj_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sale_doc_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sale_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_doc_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    qty_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    old_unit_cost_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_unit_cost_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delta_cogs_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False, server_default="oversold_settle")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_cost_adj_tenant_product", "tenant_id", "store_id", "product_id", "created_at"),
        CheckConstraint("reason IN ('oversold_settle','backfill')", name="ck_cost_adj_reason"),
    )
