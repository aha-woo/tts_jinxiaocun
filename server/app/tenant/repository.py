"""TenantRepository：所有数据访问的唯一入口（方案 A / 方案 B 共用核心）。

为什么必须经过它
----------------
- 方案 A（PG + RLS）：DB 是最后防线，但 SQL 里漏写 tenant_id 时**应用层也要挡住**；
- 方案 B（无 RLS）：它是**唯一**的隔离手段，没有它就等于数据裸奔。

行为约定
--------
1. 缺租户上下文 → :class:`TenantContextMissing`（映射 ``E_TENANT_DENIED``），**绝不查全表**；
2. 查询自动追加 ``WHERE tenant_id = :ctx.tenant_id``（与 ORM 全局 scope 双保险）；
3. 写入自动填充 ``tenant_id`` / ``store_id``，且拒绝写入他租户实体；
4. 平台级表（``goods_item`` / ``clarifier_templates``）走 :class:`PlatformRepository`，
   不参与租户过滤，也不允许被当成业务表使用；
5. 三层表（``template_rule`` / ``template_candidate`` / ``num_normalization_rules``）
   的租户列可空：默认只看本租户，``include_platform=True`` 时并看平台层行。
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.db.base import PLATFORM_TABLES, Base
from app.tenant.context import TenantContext, TenantContextMissing, require

T = TypeVar("T", bound=Base)


def _has_column(model: type[Base], name: str) -> bool:
    """模型是否有该列。"""
    return name in model.__table__.columns


def _pk_column_name(model: type[Base]) -> str:
    """主键列名（本项目每张表 PK 名不同，不能假设都叫 id）。"""
    primary_key = list(model.__table__.primary_key.columns)
    if len(primary_key) != 1:  # pragma: no cover - 本项目无复合主键
        raise ValueError(f"{model.__name__} 必须恰有一个主键列")
    return primary_key[0].name


class PlatformRepository(Generic[T]):
    """平台级表仓储（无 tenant_id）：仅限 :data:`PLATFORM_TABLES`。"""

    def __init__(self, session: Session, model: type[T]) -> None:
        table_name = model.__table__.name
        if table_name not in PLATFORM_TABLES:
            raise ValueError(
                f"{table_name} 不是平台级表，禁止使用 PlatformRepository（请用 TenantRepository）"
            )
        self.session = session
        self.model = model
        self._pk = _pk_column_name(model)

    def get(self, pk_value: Any) -> T | None:  # noqa: ANN401
        """按主键取平台行。"""
        return self.session.get(self.model, pk_value)

    def list(self, *, limit: int = 200, offset: int = 0) -> Sequence[T]:
        """列出平台行（道具/话术量级很小，默认上限 200）。"""
        statement = select(self.model).limit(limit).offset(offset)
        return list(self.session.scalars(statement).all())


class TenantRepository(Generic[T]):
    """租户级仓储：**强制** scope，缺上下文即报错。"""

    def __init__(
        self,
        session: Session,
        model: type[T],
        context: TenantContext | None = None,
        *,
        include_platform: bool = False,
    ) -> None:
        table_name = model.__table__.name
        if table_name in PLATFORM_TABLES:
            raise ValueError(f"{table_name} 是平台级表，请使用 PlatformRepository")
        if not _has_column(model, "tenant_id"):
            raise ValueError(f"{table_name} 没有 tenant_id 列，不能使用 TenantRepository")
        self.session = session
        self.model = model
        self._ctx = context or require()
        self._pk = _pk_column_name(model)
        self._include_platform = include_platform

    @property
    def context(self) -> TenantContext:
        """当前上下文（只读）。"""
        return self._ctx

    def _tenant_filter(self) -> Any:  # noqa: ANN401
        """租户过滤条件（三层表的平台行可并看）。"""
        column = self.model.__table__.c.tenant_id
        if self._include_platform:
            return or_(column.is_(None), column == self._ctx.tenant_id)
        return column == self._ctx.tenant_id

    def select(self) -> Select[Any]:
        """已带租户条件的 SELECT（自定义查询的唯一起点）。"""
        return select(self.model).where(self._tenant_filter())

    def get(self, pk_value: Any) -> T | None:  # noqa: ANN401
        """按主键取本租户行；他租户行视为不存在（不泄露存在性）。"""
        statement = self.select().where(getattr(self.model, self._pk) == pk_value)
        return self.session.scalars(statement).first()

    def require(self, pk_value: Any) -> T:  # noqa: ANN401
        """按主键取本租户行，不存在抛 ``E_NOT_FOUND``。"""
        entity = self.get(pk_value)
        if entity is None:
            raise AppError(
                "E_NOT_FOUND",
                message=f"{self.model.__tablename__} 记录不存在或不属于当前租户",
                data={"model": self.model.__tablename__, "id": str(pk_value)},
            )
        return entity

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
        descending: bool = False,
        **filters: Any,
    ) -> list[T]:
        """带租户条件的列表查询（过滤字段必须在模型中存在）。"""
        statement = self.select()
        for field_name, value in filters.items():
            if not _has_column(self.model, field_name):
                raise ValueError(f"{self.model.__tablename__} 无字段 {field_name}")
            column = getattr(self.model, field_name)
            statement = statement.where(column.is_(None) if value is None else column == value)
        if order_by is not None:
            if not _has_column(self.model, order_by):
                raise ValueError(f"{self.model.__tablename__} 无字段 {order_by}")
            column = getattr(self.model, order_by)
            statement = statement.order_by(column.desc() if descending else column.asc())
        else:
            statement = statement.order_by(getattr(self.model, self._pk).desc() if descending else getattr(self.model, self._pk).asc())
        statement = statement.limit(max(1, min(limit, 1000))).offset(max(0, offset))
        return list(self.session.scalars(statement).all())

    def count(self, **filters: Any) -> int:
        """带租户条件的计数。"""
        statement = select(func.count()).select_from(self.model).where(self._tenant_filter())
        for field_name, value in filters.items():
            if not _has_column(self.model, field_name):
                raise ValueError(f"{self.model.__tablename__} 无字段 {field_name}")
            column = getattr(self.model, field_name)
            statement = statement.where(column.is_(None) if value is None else column == value)
        return int(self.session.scalar(statement) or 0)

    def exists(self, **filters: Any) -> bool:
        """是否存在（跨租户不可见）。"""
        return self.count(**filters) > 0

    def add(self, entity: T) -> T:
        """新增：自动填 tenant_id / store_id，并拒绝他租户实体。"""
        if not isinstance(entity, self.model):
            raise TypeError(f"期望 {self.model.__name__}，收到 {type(entity).__name__}")
        entity_tenant = getattr(entity, "tenant_id", None)
        if entity_tenant is None:
            entity.tenant_id = self._ctx.tenant_id
        elif uuid.UUID(str(entity_tenant)) != self._ctx.tenant_id:
            raise AppError(
                "E_TENANT_DENIED",
                message="跨租户写入被拒绝",
                data={"entity_tenant_id": str(entity_tenant), "ctx_tenant_id": str(self._ctx.tenant_id)},
            )
        if _has_column(self.model, "store_id") and getattr(entity, "store_id", None) is None:
            entity.store_id = self._ctx.store_id
        self.session.add(entity)
        return entity

    def update(self, entity: T, **values: Any) -> T:
        """更新：先校验归属；禁止改 tenant_id。"""
        if "tenant_id" in values:
            raise ValueError("禁止修改 tenant_id")
        self._assert_owned(entity)
        for field_name, value in values.items():
            if not _has_column(self.model, field_name):
                raise ValueError(f"{self.model.__tablename__} 无字段 {field_name}")
            setattr(entity, field_name, value)
        return entity

    def delete(self, entity: T) -> None:
        """删除：先校验归属（业务上多数表用「停用/反做单」，物理删除只用于销户）。"""
        self._assert_owned(entity)
        self.session.delete(entity)

    def _assert_owned(self, entity: T) -> None:
        """断言实体属于当前租户。"""
        if not isinstance(entity, self.model):
            raise TypeError(f"期望 {self.model.__name__}，收到 {type(entity).__name__}")
        entity_tenant = getattr(entity, "tenant_id", None)
        if entity_tenant is None or uuid.UUID(str(entity_tenant)) != self._ctx.tenant_id:
            raise AppError(
                "E_TENANT_DENIED",
                message="跨租户操作被拒绝",
                data={"model": self.model.__tablename__},
            )

    def export_rows(self, *, limit: int = 10_000) -> list[dict[str, Any]]:
        """导出用行集：强制带租户条件，**不含任何跨租户数据**（T04 导出 Job 复用）。

        Returns:
            每个元素是「列名 -> 值」的扁平字典（值保持 Python 原生类型：
            ``Decimal`` / ``date`` / ``uuid.UUID``），由导出层负责序列化。
        """
        statement = self.select().limit(max(1, limit))
        entities = self.session.scalars(statement).all()
        return [entity.to_dict() for entity in entities]


def tenant_repository(
    session: Session,
    model: type[T],
    *,
    include_platform: bool = False,
) -> TenantRepository[T]:
    """工厂：按上下文构造租户仓储（推荐用法，避免到处 import 类）。"""
    context = require()
    return TenantRepository(session, model, context, include_platform=include_platform)


__all__ = [
    "PlatformRepository",
    "TenantRepository",
    "TenantContextMissing",
    "tenant_repository",
]
