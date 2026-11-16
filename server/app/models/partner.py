"""往来单位：partner / partner_alias（**租户级共享**，欠款按租户汇总、门店分摊）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, created_at_column, uuid_pk


class Partner(Base, TenantMixin):
    """客户 / 供应商。type ∈ {customer, supplier}。"""

    __tablename__ = "partner"

    partner_id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "type", "name", name="uq_partner_tenant_id_type_name"),
        Index("ix_partner_tenant_id_type_name", "tenant_id", "type", "name"),
        CheckConstraint("type IN ('customer','supplier')", name="ck_partner_type"),
        CheckConstraint("status IN ('active','disabled')", name="ck_partner_status"),
    )


class PartnerAlias(Base, TenantMixin):
    """客户/供应商别名（「南门老赵」「二中门口那家」）。"""

    __tablename__ = "partner_alias"

    alias_id: Mapped[uuid.UUID] = uuid_pk()
    partner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    member_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    alias_text: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="personal")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "alias_text", name="uq_partner_alias_tenant_id_member_id_alias_text"
        ),
        Index("ix_partner_alias_tenant_id_alias_text", "tenant_id", "alias_text"),
        CheckConstraint("source IN ('personal','industry','global')", name="ck_partner_alias_source"),
    )
