"""商品档案：product / product_alias / unit_conversion（**租户级共享，不按门店拆**）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, created_at_column, uuid_pk


class Product(Base, TenantMixin):
    """商品档案。价格一律整数「分」（`*_fen`），NULL 表示待补（绝不用 0 代替缺价）。"""

    __tablename__ = "product"

    product_id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    #: 基本单位（折算目标，见 packages/shared/contracts/slots.json 的 U 槽位）
    base_unit: Mapped[str] = mapped_column(String(16), nullable=False, server_default="个")
    #: 最近进价/售价（分，NULL=待补，0=明确免费/赠品）
    last_purchase_price_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_sale_price_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 移动加权平均成本（分，基本单位口径，NULL=暂无成本）
    avg_cost_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 商品级批次跟踪开关（默认关闭；水产/生鲜等按需开启）
    track_batch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: 商品级负库存覆盖（NULL=跟随租户设置）
    allow_negative: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 低库存阈值（基本单位）
    low_stock_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    #: 临期提醒提前天数
    expiry_warn_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: 成本法：avg（移动平均）| batch（批次实际成本）| 兼容历史 fifo/fefe/fefo
    cost_method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "spec", name="uq_product_tenant_id_name_spec"),
        Index("ix_product_tenant_id_status_name", "tenant_id", "status", "name"),
        CheckConstraint(
            "status IN ('draft','active','archived','merged','disabled')",
            name="ck_product_status",
        ),
        CheckConstraint(
            "(last_purchase_price_fen IS NULL OR last_purchase_price_fen >= 0) "
            "AND (last_sale_price_fen IS NULL OR last_sale_price_fen >= 0)",
            name="ck_product_price_nonneg",
        ),
    )


class ProductAlias(Base, TenantMixin):
    """商品别名（个人优先于租户/行业/全局）。"""

    __tablename__ = "product_alias"

    alias_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 空 = 租户级别名；非空 = 员工的个人说法
    member_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    alias_text: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="personal")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "member_id", "alias_text", name="uq_product_alias_tenant_id_member_id_alias_text"),
        Index("ix_product_alias_tenant_id_alias_text", "tenant_id", "alias_text"),
        CheckConstraint("source IN ('personal','industry','global')", name="ck_product_alias_source"),
    )


class UnitConversion(Base, TenantMixin):
    """单位换算（`ratio` **不可臆造**，来自档案且需审核；R3 审核后生效）。"""

    __tablename__ = "unit_conversion"

    conv_id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    to_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 1 from_unit = ratio × to_unit；只由人工审核写入（代码绝不硬编码）
    ratio: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "product_id", "from_unit", "to_unit", name="uq_unit_conversion_tenant_id_product_id_from_unit_to_unit"
        ),
        Index("ix_unit_conversion_tenant_id_product_id_from_unit", "tenant_id", "product_id", "from_unit"),
        CheckConstraint("ratio > 0", name="ck_unit_conversion_ratio_positive"),
        CheckConstraint("status IN ('pending','approved')", name="ck_unit_conversion_status"),
    )
