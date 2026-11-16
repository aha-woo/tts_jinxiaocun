"""配额与计费：usage_quota / usage_ledger。

双口径（架构 §4.4，两者**分别存储、前台不得混用**）：
- `usage_quota.period = YYYY-MM` → 自然月用量归集与成本归因；
- `entitlement.valid_from/valid_until`（30 天滚动窗口）→ 权益有效期。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String, UniqueConstraint, func
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, created_at_column, uuid_pk


class UsageQuota(Base, TenantMixin):
    """租户 × 自然月 的额度行（一行一租户一月）。"""

    __tablename__ = "usage_quota"

    quota_id: Mapped[uuid.UUID] = uuid_pk()
    #: YYYY-MM（自然月归集口径，**不是**权益有效期）
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    free_quota: Mapped[int] = mapped_column(Integer, nullable=False, server_default="150")
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: L0~L4：降级但不停止服务（PRD 5.6）
    degrade_level: Mapped[str] = mapped_column(String(2), nullable=False, server_default="L0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_quota_tenant_id_period"),
        Index("ix_usage_quota_tenant_id_period", "tenant_id", "period"),
        CheckConstraint("used_count >= 0 AND free_quota >= 0", name="ck_usage_quota_counts"),
        CheckConstraint("degrade_level IN ('L0','L1','L2','L3','L4')", name="ck_usage_quota_degrade_level"),
    )


class UsageLedger(Base, TenantMixin):
    """计费流水：`UNIQUE(tenant_id, session_id, event_type)` 是**计费幂等核心**。

    仅「卡片生成成功」写入 `CARD_CREATED`；门禁/过滤/失败/取消**不写**。
    发货（虚拟支付）**绝不写本表**（权益发放与语音消耗分离，A10）。
    """

    __tablename__ = "usage_ledger"

    ledger_id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="CARD_CREATED")
    #: 成本归因（分）；【假设，需核实】实际单价在配置中
    cost_estimate_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "event_type", name="uq_usage_ledger_tenant_id_session_id_event_type"),
        Index("ix_usage_ledger_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint("event_type IN ('CARD_CREATED')", name="ck_usage_ledger_event_type"),
    )
