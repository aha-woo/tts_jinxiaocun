"""规则层配置：template_rule / template_candidate / clarifier_templates / num_normalization_rules。

层级（架构 §4.3.2）：
- `template_rule` / `template_candidate`：**平台层（tenant_id IS NULL）+ 租户层**并存；
- `clarifier_templates`：**纯平台级**，无 tenant_id（静态话术，不调 AI）；
- `num_normalization_rules`：三层（global / industry / personal），租户与员工均可空。

因此唯一定义用「tenant_id 打头的复合唯一约束 + 平台层部分唯一索引」双管：
PG/SQLite 都把 NULL 视为互不相等，只靠复合约束无法阻止平台层重复。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, NullableTenantMixin, created_at_column, uuid_pk


class TemplateRule(Base, NullableTenantMixin):
    """规则模板（T03 的 G3 层输入）；`version` + `pattern_hash` 保证可回归、可灰度。"""

    __tablename__ = "template_rule"

    rule_id: Mapped[uuid.UUID] = uuid_pk()
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    #: 模板文本归一化后的哈希（去重与回归用）
    pattern_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "version", "intent_id", "pattern_hash",
            name="uq_template_rule_tenant_id_version_intent_id_pattern_hash",
        ),
        Index("ix_template_rule_intent_id_priority_enabled", "intent_id", "priority", "enabled"),
        Index(
            "uq_template_rule_platform",
            "version",
            "intent_id",
            "pattern_hash",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
            sqlite_where=text("tenant_id IS NULL"),
        ),
    )


class TemplateCandidate(Base, NullableTenantMixin):
    """纠错回流生成的候选模板：**必须人工审核后**才可生效（防污染，D6）。"""

    __tablename__ = "template_candidate"

    candidate_id: Mapped[uuid.UUID] = uuid_pk()
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "version", "intent_id", "pattern_hash",
            name="uq_template_candidate_tenant_id_version_intent_id_pattern_hash",
        ),
        Index("ix_template_candidate_tenant_id_status", "tenant_id", "status"),
        Index(
            "uq_template_candidate_platform",
            "version",
            "intent_id",
            "pattern_hash",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
            sqlite_where=text("tenant_id IS NULL"),
        ),
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_template_candidate_status"),
    )


class ClarifierTemplate(Base):
    """追问话术（**平台级，无 tenant_id**）：静态内容，澄清时不调 AI。"""

    __tablename__ = "clarifier_templates"

    clarifier_id: Mapped[uuid.UUID] = uuid_pk()
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 置信度分档（呼应 PRD 4.2；band 为配置化字符串，不做硬编码判断）
    band: Mapped[str] = mapped_column(String(16), nullable=False, server_default="default")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("intent_id", "slot_code", "band", name="uq_clarifier_templates_intent_id_slot_code_band"),
        Index("ix_clarifier_templates_intent_id_slot_code_band", "intent_id", "slot_code", "band"),
    )


class NumNormalizationRule(Base, NullableTenantMixin):
    """数字/金额/单位归一规则（三层：global / industry / personal）。"""

    __tablename__ = "num_normalization_rules"

    rule_id: Mapped[uuid.UUID] = uuid_pk()
    member_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    layer: Mapped[str] = mapped_column(String(16), nullable=False, server_default="global")
    pattern: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "layer", "member_id", "pattern",
            name="uq_num_normalization_rules_tenant_id_layer_member_id_pattern",
        ),
        Index("ix_num_normalization_rules_layer_priority_pattern", "layer", "priority", "pattern"),
        Index(
            "uq_num_normalization_rules_platform",
            "layer",
            "member_id",
            "pattern",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
            sqlite_where=text("tenant_id IS NULL"),
        ),
        CheckConstraint("layer IN ('global','industry','personal')", name="ck_num_norm_layer"),
    )
