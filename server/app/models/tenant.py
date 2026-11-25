"""租户根表（计费与数据归属最小单位）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column, uuid_pk


class Tenant(Base):
    """租户。**根表**：`tenant_id` 即主键，无上级租户。"""

    __tablename__ = "tenant"

    tenant_id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False, server_default="free")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    #: 月度额度（自然月归集口径，**假设，需核实**）
    quota_monthly: Mapped[int] = mapped_column(Integer, nullable=False, server_default="150")
    #: 租户版本号：禁用/降级时 +1，旧 JWT 立即失效（见 services/auth.py）
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: 默认允许负库存（允许后超卖按暂估成本记、补进货自动结清）
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: 记账模式：per_txn（逐笔进销）| count_derive（盘点倒推）
    accounting_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="per_txn")
    #: 是否允许店员看进价和毛利（默认仅老板可见）
    staff_can_view_cost: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("status IN ('active','frozen','deleting')", name="ck_tenant_status"),
        CheckConstraint("plan_code IN ('free','30day','quota_pack')", name="ck_tenant_plan"),
        CheckConstraint("accounting_mode IN ('per_txn','count_derive')", name="ck_tenant_accounting_mode"),
        Index("ix_tenant_status", "status"),
    )
