"""词表与纠错：personal_dict / correction_event / reversal。

- `personal_dict` 是**员工级**（个人说法优先），`version` 击穿解析缓存；
- `correction_event` 是纠错回流的原始素材（T03 生成候选模板，T04 审核生效）；
- `reversal` 是反做单记录：**不删原单**，只记录「原单 ↔ 反做单」映射。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, StoreScopedMixin, TenantMixin, created_at_column, uuid_pk


class PersonalDict(Base, TenantMixin):
    """个人词表（「快乐水」= 可乐）。员工级 → 只对该员工生效。"""

    __tablename__ = "personal_dict"

    dict_id: Mapped[uuid.UUID] = uuid_pk()
    member_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    layer: Mapped[str] = mapped_column(String(16), nullable=False, server_default="personal")
    raw_text: Mapped[str] = mapped_column(String(64), nullable=False)
    mapped_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mapped_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "raw_text", "mapped_type",
            name="uq_personal_dict_tenant_id_member_id_raw_text_mapped_type",
        ),
        Index("ix_personal_dict_tenant_id_member_id_raw_text", "tenant_id", "member_id", "raw_text"),
        CheckConstraint("mapped_type IN ('product','partner','number','unit','intent')", name="ck_personal_dict_mapped_type"),
    )


class CorrectionEvent(Base, TenantMixin):
    """纠错事件：用户改卡片 → 本地重算（不重调 AI）→ 回流成候选素材。"""

    __tablename__ = "correction_event"

    event_id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    slot_code: Mapped[str] = mapped_column(String(8), nullable=False)
    original_fragment: Mapped[str | None] = mapped_column(Text, nullable=True)
    wrong_value: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    correct_value: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_correction_event_tenant_id_session_id", "tenant_id", "session_id"),
        Index("ix_correction_event_tenant_id_created_at", "tenant_id", "created_at"),
    )


class Reversal(Base, TenantMixin, StoreScopedMixin):
    """反做单映射：撤销 = 生成反做单，**原单保留**（PRD 3.8 / T04）。"""

    __tablename__ = "reversal"

    reversal_id: Mapped[uuid.UUID] = uuid_pk()
    original_doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    reversal_doc_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "original_doc_id", "reversal_doc_id", name="uq_reversal_tenant_id_original_doc_id_reversal_doc_id"
        ),
        Index("ix_reversal_tenant_id_original_doc_id", "tenant_id", "original_doc_id"),
    )
