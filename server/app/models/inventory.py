"""库存（**门店级**）：qty_base 折算到商品基本单位；version 用于影响预演 CAS。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, StoreScopedMixin, TenantMixin, uuid_pk


class Inventory(Base, TenantMixin, StoreScopedMixin):
    """门店 × 商品 的库存行。"""

    __tablename__ = "inventory"

    inventory_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 折算到 base_unit 的数量（不用 float 存数量）
    qty_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    #: CAS 乐观锁：落账时校验 preview_version，冲突 -> E_PREVIEW_VERSION_CONFLICT
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "product_id", name="uq_inventory_tenant_id_store_id_product_id"),
        Index("ix_inventory_tenant_id_store_id_product_id", "tenant_id", "store_id", "product_id"),
        CheckConstraint("qty_base >= 0", name="ck_inventory_qty_nonneg"),
    )
