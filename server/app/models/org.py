"""组织：门店 / 员工 / 角色（租户 → 门店 → 员工 → 角色）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, created_at_column, uuid_pk


class Store(Base, TenantMixin):
    """门店/仓库。MVP 仅 1 行主仓；升级多店不迁移数据（H16）。"""

    __tablename__ = "store"

    store_id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_store_tenant_id_name"),
        Index("ix_store_tenant_id_is_main", "tenant_id", "is_main"),
        CheckConstraint("status IN ('active','disabled')", name="ck_store_status"),
    )


class Member(Base, TenantMixin):
    """员工（微信身份）。openid 来自 wx.login，**不做账号密码**（D15）。"""

    __tablename__ = "member"

    member_id: Mapped[uuid.UUID] = uuid_pk()
    wx_openid: Mapped[str] = mapped_column(String(64), nullable=False)
    wx_unionid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default="老板")
    #: 加密存储（字段级脱敏由 API 序列化层按 role 处理，PRD 7.2）
    phone_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    #: 按人计的新手期语音入账数与一次过数（前 20 笔必出卡）
    novice_voice_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    novice_first_pass_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 是否启用直接记（A 档）
    auto_post_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "wx_openid", name="uq_member_tenant_id_wx_openid"),
        Index("ix_member_tenant_id_wx_openid", "tenant_id", "wx_openid"),
        CheckConstraint("status IN ('active','disabled')", name="ck_member_status"),
    )


class Role(Base, TenantMixin):
    """角色：boss / manager / clerk / readonly；store_id 为空表示全店。"""

    __tablename__ = "role"

    role_id: Mapped[uuid.UUID] = uuid_pk()
    member_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    role_code: Mapped[str] = mapped_column(String(16), nullable=False, server_default="boss")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "store_id", "role_code", name="uq_role_tenant_id_member_id_store_id_role_code"
        ),
        Index("ix_role_tenant_id_member_id", "tenant_id", "member_id"),
        CheckConstraint(
            "role_code IN ('boss','manager','clerk','readonly')",
            name="ck_role_role_code",
        ),
    )


#: 角色枚举（与 guards.py 保持一致）
ROLE_CODES: tuple[str, ...] = ("boss", "manager", "clerk", "readonly")
