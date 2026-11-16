"""租户/角色守卫：写操作前的归属断言（越权直接抛错，不做「尽量兜住」）。

对应错误码（不新增）：
- 跨租户访问/写入 → ``E_TENANT_DENIED``(1301, 403)
- 角色不足 → ``E_ROLE_DENIED``(1302, 403)
- 租户被冻结/销户中 → ``E_TENANT_DENIED``（带状态说明）
"""

from __future__ import annotations

import uuid
from typing import Iterable

from app.common.errors import AppError
from app.tenant.context import TenantContext, require

#: 角色枚举（PRD §7.2）：老板 / 店长 / 店员 / 只读
ROLE_BOSS = "boss"
ROLE_MANAGER = "manager"
ROLE_CLERK = "clerk"
ROLE_READONLY = "readonly"
ALL_ROLES = frozenset({ROLE_BOSS, ROLE_MANAGER, ROLE_CLERK, ROLE_READONLY})

#: 允许写业务数据的角色（readonly 一律只读）
WRITE_ROLES = frozenset({ROLE_BOSS, ROLE_MANAGER, ROLE_CLERK})

#: R3 白名单动作只允许老板（且仅电脑版微信大屏管理页，见 T04）
R3_ROLES = frozenset({ROLE_BOSS})


def require_role(allowed: Iterable[str], context: TenantContext | None = None) -> TenantContext:
    """断言当前角色在允许集合内。

    Raises:
        AppError: ``E_ROLE_DENIED``（403）
    """
    ctx = context or require()
    allow = frozenset(allowed)
    if ctx.role_code not in allow:
        raise AppError(
            "E_ROLE_DENIED",
            message=f"当前角色 {ctx.role_code} 无此权限（需要 {sorted(allow)}）",
            data={"role_code": ctx.role_code, "required": sorted(allow)},
        )
    return ctx


def require_writable(context: TenantContext | None = None) -> TenantContext:
    """断言可写：角色 ∈ {boss, manager, clerk}。"""
    return require_role(WRITE_ROLES, context)


def require_r3(context: TenantContext | None = None) -> TenantContext:
    """断言 R3 白名单动作：仅 boss（T04 还会校验来源为大屏管理页）。"""
    return require_role(R3_ROLES, context)


def assert_same_tenant(entity: object, context: TenantContext | None = None, action: str = "访问") -> None:
    """断言实体归属与当前租户一致。

    Args:
        entity: 带 ``tenant_id`` 属性的 ORM 实例（平台级实体请勿调用本函数）。
        context: 可选上下文（默认取当前）。
        action: 用于错误文案的动作说明。

    Raises:
        AppError: ``E_TENANT_DENIED``（403）
    """
    ctx = context or require()
    entity_tenant = getattr(entity, "tenant_id", None)
    if entity_tenant is None:
        raise AppError(
            "E_TENANT_DENIED",
            message=f"实体缺少 tenant_id，禁止{action}",
            data={"action": action},
        )
    if uuid.UUID(str(entity_tenant)) != ctx.tenant_id:
        raise AppError(
            "E_TENANT_DENIED",
            message=f"跨租户{action}被拒绝",
            data={"action": action, "entity_tenant_id": str(entity_tenant)},
        )


def assert_tenant_active(status: str, context: TenantContext | None = None) -> None:
    """断言租户状态可用（active / frozen / deleting）。"""
    ctx = context or require()
    if status != "active":
        raise AppError(
            "E_TENANT_DENIED",
            message=f"租户状态为 {status}，暂不可写",
            data={"tenant_status": status, "tenant_id": str(ctx.tenant_id)},
        )


def guard_write(entity: object, context: TenantContext | None = None) -> TenantContext:
    """写操作统一入口：先校验角色可写，再校验实体归属。"""
    ctx = require_writable(context)
    assert_same_tenant(entity, ctx, action="写入")
    return ctx
