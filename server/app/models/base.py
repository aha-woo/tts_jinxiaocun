"""模型公共件：Base / Mixin / 跨方言类型 / 建表清单。

34 张表（架构 §4.3.2 的 33 行清单中 `template_rule` 与 `template_candidate` 同处一行，
实际表数为 34）按域拆分到同目录各文件，导入本包即完成全部注册。
"""

from app.db.base import (
    Base,
    JSONType,
    NullableTenantMixin,
    PLATFORM_TABLES,
    StoreScopedMixin,
    TenantMixin,
    TimestampsMixin,
    created_at_column,
    uuid_pk,
)

__all__ = [
    "Base",
    "JSONType",
    "PLATFORM_TABLES",
    "TenantMixin",
    "NullableTenantMixin",
    "StoreScopedMixin",
    "TimestampsMixin",
    "created_at_column",
    "uuid_pk",
]
