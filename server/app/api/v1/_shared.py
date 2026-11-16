"""v1 路由共享件：租户身份提取、角色校验、销户冻结校验、语音运行时装配。

为什么集中在这里
--------------
13 个路由文件如果各自写一遍「取 tenant_id / 判角色 / 判冻结 / 装流水线」，
迟早会出现「某个接口忘了判冻结」「某个接口用了别的 tenant_id 来源」这种漏洞。
**入参永远不传 tenant_id**（防伪造），一律从 JWT 的上下文取——这里只留一个入口。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import Settings
from app.models.org import Store
from app.services.deletion_request import DeletionService
from app.services.voice_runtime import VoiceRuntime, build_runtime
from app.tenant.context import TenantContext

__all__ = [
    "BIG_SCREEN_HINT",
    "BOSS_ONLY",
    "WRITE_ROLES",
    "IdSet",
    "assert_writable",
    "ids_of",
    "require_roles",
    "resolve_store_name",
    "runtime_for",
]

#: 允许写操作的角色的粗筛（细粒度权限在 T05 大屏管理页再细分）
WRITE_ROLES: tuple[str, ...] = ("boss", "manager", "clerk")

#: 只有老板能做的动作（导出 / 审批 / 销户）
BOSS_ONLY: tuple[str, ...] = ("boss",)

#: R3 动作的统一引导话术（**不引导外部浏览器 / H5**：本项目无网页端）
BIG_SCREEN_HINT = "该动作只能在电脑版微信大屏管理页确认（同一微信登录，需 role=boss 二次确认）"


@dataclass(frozen=True)
class IdSet:
    """一次请求里的租户身份（全部来自服务端 JWT）。"""

    tenant_id: uuid.UUID
    store_id: uuid.UUID
    member_id: uuid.UUID
    role: str


def ids_of(context: TenantContext) -> IdSet:
    """从租户上下文取身份要素。"""
    return IdSet(
        tenant_id=context.tenant_id,
        store_id=context.store_id,
        member_id=context.member_id,
        role=context.role_code,
    )


def require_roles(context: TenantContext, allowed: Sequence[str]) -> None:
    """角色校验。

    Raises:
        AppError: ``E_ROLE_DENIED``(403)。
    """
    role = (context.role_code or "").strip().lower()
    if role not in {item.lower() for item in allowed}:
        raise AppError(
            "E_ROLE_DENIED",
            message=f"该操作需要 {'/'.join(allowed)} 角色，当前角色 {role!r}",
            data={"required_roles": list(allowed)},
        )


def assert_writable(session: Session, tenant_id: uuid.UUID) -> None:
    """销户冷静期内只读冻结：写操作一律拒。

    Raises:
        AppError: ``E_TENANT_DENIED``(403)。
    """
    DeletionService(session).assert_writable(tenant_id=tenant_id)


def resolve_store_name(session: Session, *, tenant_id: uuid.UUID, store_id: uuid.UUID) -> str:
    """取门店名（缺省填充 ``W`` 用；取不到回「主仓」，不报错）。"""
    store = session.execute(
        select(Store)
        .where(Store.tenant_id == tenant_id, Store.store_id == store_id)
        .limit(1)
    ).scalar_one_or_none()
    if store is None:
        return "主仓"
    return str(getattr(store, "name", None) or "主仓")


def runtime_for(
    session: Session,
    settings: Settings,
    context: TenantContext,
) -> VoiceRuntime:
    """按当前租户装一条语音运行时（词表来自 DB）。"""
    ids = ids_of(context)
    return build_runtime(
        session,
        settings,
        tenant_id=ids.tenant_id,
        member_id=ids.member_id,
        store_name=resolve_store_name(
            session, tenant_id=ids.tenant_id, store_id=ids.store_id
        ),
    )
