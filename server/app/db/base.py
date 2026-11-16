"""数据层基座：声明式 Base、命名约定、跨方言类型与多租户 Mixin。

设计要点
--------
1. **PG 为主、SQLite 可跑**：`jsonb`、`uuid`、`timestamptz` 用 SQLAlchemy 的跨方言类型
   （`JSON().with_variant(JSONB, "postgresql")`、`Uuid`、`DateTime(timezone=True)`），
   生产 PostgreSQL 拿到原生类型，本地/测试 SQLite 也能建表。
2. **命名约定**：约束/索引名统一，避免 Alembic autogenerate 产生随机名。
3. **归属层级（架构 §4.3.2）**：把「是否带 tenant_id / store_id」编码成 Mixin，
   模型文件里一眼能看出层级，迁移脚本也能据此判定 RLS 覆盖范围。
4. **平台级表豁免**：`goods_item` / `clarifier_templates` 无 tenant_id，不参与 RLS；
   道具定义单一真相源仍是 `packages/shared/contracts/goods.json`（服务端只读，不复制）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

#: 约束/索引命名约定（迁移可复现，避免随机名）
#: 说明：CheckConstraint 的显式名字已自带 ``ck_<table>_`` 前缀，故 ``ck`` 只用
#: ``%(constraint_name)s``，避免渲染出 ``ck_goods_item_ck_goods_item_type`` 这种双前缀。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: 跨方言 JSON：PG 用 jsonb（可索引），SQLite 退化为 TEXT 存储的 JSON
JSONType: JSON = JSON().with_variant(JSONB(), "postgresql")

#: 平台级表（无 tenant_id，不参与 RLS）——迁移与测试共用此白名单
PLATFORM_TABLES: frozenset[str] = frozenset({"goods_item", "clarifier_templates"})

#: tenant_id 可空的三层表（平台层 + 租户层并存）：RLS 策略需容忍 NULL
NULLABLE_TENANT_TABLES: frozenset[str] = frozenset(
    {"template_rule", "template_candidate", "num_normalization_rules"}
)


class Base(DeclarativeBase):
    """所有模型的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict[str, Any]:
        """把实例转成 dict（审计/日志/测试断言用，不含 SQLAlchemy 内部状态）。"""
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


def uuid_pk(default_factory: Any = uuid.uuid4) -> Mapped[uuid.UUID]:
    """标准 UUID 主键列（PG 原生 uuid，SQLite CHAR(32)）。"""
    return mapped_column(Uuid, primary_key=True, default=default_factory)


def created_at_column() -> Mapped[datetime]:
    """创建时间（timestamptz，默认 now()）。"""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TimestampsMixin:
    """只含创建时间的表复用（多数业务表）。"""

    created_at: Mapped[datetime] = created_at_column()


class TenantMixin:
    """租户级归属：tenant_id NOT NULL + 外键到 tenant。"""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenant.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )


class NullableTenantMixin:
    """平台/租户共存归属：tenant_id 可空（NULL = 平台层行）。"""

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("tenant.tenant_id", ondelete="CASCADE"),
        nullable=True,
    )


class StoreScopedMixin:
    """门店级归属：store_id NOT NULL（租户维度由 TenantMixin 提供）。"""

    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
