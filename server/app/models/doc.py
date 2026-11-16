"""单据与应收应付：doc_header / doc_line / ar_ap / payment。

金额字段一律 `*_fen`（bigint，整数分）；`biz_date` 为业务日期（date），
时间戳为 timestamptz。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, StoreScopedMixin, TenantMixin, created_at_column, uuid_pk


class DocHeader(Base, TenantMixin, StoreScopedMixin):
    """单据表头。`doc_no` 租户内唯一且**永久不变**（升级多门店不迁移、不改号，H16）。"""

    __tablename__ = "doc_header"

    doc_id: Mapped[uuid.UUID] = uuid_pk()
    doc_no: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="已结")
    total_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    #: confirmed / reversed（撤销走反做单，原单保留）
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="confirmed")
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_no", name="uq_doc_header_tenant_id_doc_no"),
        Index("ix_doc_header_tenant_id_store_id_biz_date_status", "tenant_id", "store_id", "biz_date", "status"),
        Index("ix_doc_header_tenant_id_doc_no", "tenant_id", "doc_no"),
        CheckConstraint("total_amount_fen >= 0", name="ck_doc_header_amount_nonneg"),
        CheckConstraint("payment_status IN ('已结','未结','部分')", name="ck_doc_header_payment_status"),
        CheckConstraint("status IN ('confirmed','reversed')", name="ck_doc_header_status"),
    )


class DocLine(Base, TenantMixin, StoreScopedMixin):
    """单据行。`source_map` 记录每个槽位的 source（asr/default/computed/alias/pending）。"""

    __tablename__ = "doc_line"

    line_id: Mapped[uuid.UUID] = uuid_pk()
    doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 原话数量与单位（双显回显用）
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 折算到 base_unit 的数量（与 inventory.qty_base 同口径）
    qty_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    unit_price_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    source_map: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_doc_line_tenant_id_doc_id", "tenant_id", "doc_id"),
        CheckConstraint("amount_fen >= 0 AND unit_price_fen >= 0", name="ck_doc_line_amount_nonneg"),
        CheckConstraint("qty >= 0", name="ck_doc_line_qty_nonneg"),
    )


class ArAp(Base, TenantMixin, StoreScopedMixin):
    """应收应付（门店发生、租户汇总）。`version` 用于核销 CAS。"""

    __tablename__ = "ar_ap"

    ar_ap_id: Mapped[uuid.UUID] = uuid_pk()
    partner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    settled_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_id", "direction", name="uq_ar_ap_tenant_id_doc_id_direction"),
        Index("ix_ar_ap_tenant_id_partner_id_status", "tenant_id", "partner_id", "status"),
        CheckConstraint("direction IN ('receivable','payable')", name="ck_ar_ap_direction"),
        CheckConstraint("status IN ('open','partial','settled')", name="ck_ar_ap_status"),
        CheckConstraint(
            "amount_fen >= 0 AND settled_amount_fen >= 0 AND settled_amount_fen <= amount_fen",
            name="ck_ar_ap_amounts",
        ),
    )


class Payment(Base, TenantMixin, StoreScopedMixin):
    """收付款流水，`idempotency_key` 保证重复提交不重复记账。"""

    __tablename__ = "payment"

    payment_id: Mapped[uuid.UUID] = uuid_pk()
    ar_ap_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False, server_default="现金")
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_payment_tenant_id_idempotency_key"),
        Index("ix_payment_tenant_id_ar_ap_id_biz_date", "tenant_id", "ar_ap_id", "biz_date"),
        CheckConstraint("amount_fen > 0", name="ck_payment_amount_positive"),
    )
