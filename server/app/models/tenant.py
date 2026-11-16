"""租户根表（计费与数据归属最小单位）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, Text
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
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("status IN ('active','frozen','deleting')", name="ck_tenant_status"),
        CheckConstraint("plan_code IN ('free','30day','quota_pack')", name="ck_tenant_plan"),
        Index("ix_tenant_status", "status"),
    )
