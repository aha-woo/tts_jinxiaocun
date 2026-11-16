"""运维与合规：pending_task / audit_log / deletion_request / export_job。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TenantMixin, created_at_column, uuid_pk


class PendingTask(Base, TenantMixin):
    """R3 白名单动作的待办：语音只生成待办，**只能在电脑版微信大屏管理页**执行。"""

    __tablename__ = "pending_task"

    task_id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "intent_id", name="uq_pending_task_tenant_id_session_id_intent_id"),
        Index("ix_pending_task_tenant_id_status_intent_id", "tenant_id", "status", "intent_id"),
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_pending_task_status"),
    )


class AuditLog(Base, TenantMixin):
    """全写操作留痕（九道防线第 9 条）。"""

    __tablename__ = "audit_log"

    log_id: Mapped[uuid.UUID] = uuid_pk()
    member_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_audit_log_tenant_id_created_at_action", "tenant_id", "created_at", "action"),
    )


class DeletionRequest(Base, TenantMixin):
    """销户申请（R3，完全禁止语音）：7 天冷静期内只读冻结、可撤销。"""

    __tablename__ = "deletion_request"

    request_id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    #: 冷静期截止日（7 天【需核实合规】）
    cool_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_deletion_request_tenant_id_request_id"),
        Index("ix_deletion_request_tenant_id_status", "tenant_id", "status"),
        CheckConstraint("status IN ('pending','cancelled','completed')", name="ck_deletion_request_status"),
    )


class ExportJob(Base, TenantMixin):
    """导出任务：文件经微信转发/保存/预览交付，**不提供网页下载**（D14）。"""

    __tablename__ = "export_job"

    job_id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False, server_default="csv")
    period: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 对象存储引用（路径含 `t/{tenant_id}/` 前缀），绝不存文件本体
    file_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_export_job_tenant_id_idempotency_key"),
        Index("ix_export_job_tenant_id_status", "tenant_id", "status"),
        CheckConstraint("status IN ('queued','running','done','failed')", name="ck_export_job_status"),
        CheckConstraint("file_format IN ('csv','xlsx')", name="ck_export_job_format"),
    )
