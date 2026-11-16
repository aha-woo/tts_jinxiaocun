"""多租户基座（方案 A：PG + RLS / 方案 B：应用层强制 scope）。

模块职责
--------
- ``context``    请求级 contextvar 租户上下文（缺失即抛错）
- ``rls``        PG RLS 落地 + 隔离能力自描述（SQLite 下明确 no-op）
- ``repository`` TenantRepository：所有数据访问唯一入口
- ``cache``      缓存 key `t:{tenant_id}:` 前缀唯一拼装点
- ``guards``     写操作前的归属/角色断言
"""

from app.tenant.context import TenantContext, TenantContextMissing, require, use_context
from app.tenant.repository import PlatformRepository, TenantRepository, tenant_repository

__all__ = [
    "TenantContext",
    "TenantContextMissing",
    "require",
    "use_context",
    "TenantRepository",
    "PlatformRepository",
    "tenant_repository",
]
