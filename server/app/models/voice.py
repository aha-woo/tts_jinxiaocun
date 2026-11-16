"""语音链路：voice_session / voice_log / confirm_card。

- `voice_session` 是状态机主体与幂等键（§5.3）；
- `voice_log` **只存 asr_text，不存音频**（D8）；token 计数用于成本归因；
- `confirm_card` 存只读影响预演快照 + `preview_version`（落账 CAS）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    JSONType,
    StoreScopedMixin,
    TenantMixin,
    created_at_column,
    uuid_pk,
)


class VoiceSession(Base, TenantMixin, StoreScopedMixin):
    """语音会话。状态枚举见 shared/schemas/voice-session.schema.json（16 态）。"""

    __tablename__ = "voice_session"

    session_id: Mapped[uuid.UUID] = uuid_pk()
    member_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="RECORDING")
    clarify_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 卡片 30s TTL【假设，需核实】
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 音频引用（临时 fileID/URL），绝不是音频本体；用后限期删除
    audio_object_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", name="uq_voice_session_tenant_id_session_id"),
        Index("ix_voice_session_tenant_id_member_id_state_expires_at", "tenant_id", "member_id", "state", "expires_at"),
        CheckConstraint("clarify_round >= 0 AND clarify_round <= 2", name="ck_voice_session_clarify_round"),
    )


class VoiceLog(Base, TenantMixin, StoreScopedMixin):
    """ASR 与解析留痕（不存音频；文本按合规限期清理【需核实】）。"""

    __tablename__ = "voice_log"

    log_id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    asr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    asr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    parse_channel: Mapped[str] = mapped_column(String(16), nullable=False, server_default="template")
    parse_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    llm_tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    llm_tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", name="uq_voice_log_tenant_id_session_id"),
        Index("ix_voice_log_tenant_id_session_id", "tenant_id", "session_id"),
        CheckConstraint("parse_channel IN ('template','llm','filtered')", name="ck_voice_log_parse_channel"),
    )


class ConfirmCard(Base, TenantMixin):
    """确认卡片（7 区块）：任何写操作必经此处，禁止绕过（T04）。"""

    __tablename__ = "confirm_card"

    card_id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    slots: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: 只读预演快照（不含副作用），落账时以 preview_version 做 CAS
    impact_preview: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    calc_traces: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(4), nullable=False, server_default="R1")
    confirm_policy: Mapped[str] = mapped_column(String(4), nullable=False, server_default="C1")
    preview_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", name="uq_confirm_card_tenant_id_session_id"),
        Index("ix_confirm_card_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint("risk_level IN ('R0','R1','R2','R3')", name="ck_confirm_card_risk"),
        CheckConstraint("confirm_policy IN ('C0','C1','C2','C3')", name="ck_confirm_card_policy"),
        CheckConstraint("status IN ('active','confirmed','cancelled','expired','pending_task')", name="ck_confirm_card_status"),
    )
