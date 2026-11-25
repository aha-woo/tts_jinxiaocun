"""当前用户画像与店铺偏好：`GET /api/v1/me`、`PATCH /api/v1/me/preferences`。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.v1._shared import (
    BOSS_ONLY,
    assert_writable,
    can_view_cost,
    ids_of,
    require_roles,
    resolve_store_name,
)
from app.common.envelope import ok
from app.deps import AuthContextDep, TenantSessionDep
from app.models.catalog import ProductAlias
from app.models.dict import PersonalDict
from app.models.org import Member, Role
from app.models.tenant import Tenant
from app.services.audit import AuditService
from app.services.novice import novice_status_of

router = APIRouter(prefix="/me", tags=["me"])


class MePreferencesPatch(BaseModel):
    """个人与门店偏好开关。"""

    model_config = ConfigDict(extra="forbid")

    auto_post_enabled: bool | None = None
    accounting_mode: Literal["per_txn", "count_derive"] | None = None
    display_name: str | None = None


@router.get("", summary="当前账号、新手期进度、店员与词表概览")
def get_me(
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """返回当前登录成员的真实资料与店员列表（取代前端写死 Mock 文案）。"""
    ids = ids_of(context)
    member = session.get(Member, ids.member_id)
    tenant = session.get(Tenant, ids.tenant_id)
    store_name = resolve_store_name(session, tenant_id=ids.tenant_id, store_id=ids.store_id)

    personal_count = int(
        session.execute(
            select(func.count())
            .select_from(PersonalDict)
            .where(
                PersonalDict.tenant_id == ids.tenant_id,
                PersonalDict.member_id == ids.member_id,
            )
        ).scalar_one()
        or 0
    )
    product_alias_count = int(
        session.execute(
            select(func.count())
            .select_from(ProductAlias)
            .where(ProductAlias.tenant_id == ids.tenant_id)
        ).scalar_one()
        or 0
    )

    role_lower = (ids.role or "").strip().lower()
    if role_lower in ("boss", "manager"):
        members_rows = list(
            session.execute(
                select(Member, Role.role_code)
                .outerjoin(Role, Role.member_id == Member.member_id)
                .where(Member.tenant_id == ids.tenant_id)
            ).all()
        )
        seen_members: dict[str, dict[str, Any]] = {}
        for m_row, r_code in members_rows:
            mid = str(m_row.member_id)
            if mid not in seen_members:
                seen_members[mid] = {
                    "member_id": mid,
                    "name": str(m_row.name),
                    "role": str(r_code or "clerk"),
                    "status": str(m_row.status),
                    "is_self": m_row.member_id == ids.member_id,
                }
        members_list = list(seen_members.values())
    else:
        members_list = (
            [
                {
                    "member_id": str(member.member_id),
                    "name": str(member.name),
                    "role": role_lower or "clerk",
                    "status": str(member.status),
                    "is_self": True,
                }
            ]
            if member is not None
            else []
        )

    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "member_id": str(ids.member_id),
                "tenant_id": str(ids.tenant_id),
                "store_id": str(ids.store_id),
                "role": ids.role,
                "display_name": str(member.name) if member is not None else "店主",
                "store_name": store_name,
                "plan_code": str(tenant.plan_code) if tenant is not None else "free",
                "accounting_mode": str(tenant.accounting_mode) if tenant is not None else "per_txn",
                "allow_negative": bool(tenant.allow_negative) if tenant is not None else True,
                "staff_can_view_cost": bool(tenant.staff_can_view_cost) if tenant is not None else False,
                "can_view_cost": can_view_cost(session, context),
                "novice": novice_status_of(member),
                "auto_post_enabled": bool(member.auto_post_enabled) if member is not None else False,
                "alias_count": personal_count + product_alias_count,
                "members": members_list,
            }
        ),
    )


@router.patch("/preferences", summary="修改确认方式与记账方式偏好")
def patch_preferences(
    payload: MePreferencesPatch,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """更新成员直记开关（A 档）及老板记账方式（逐笔 vs 盘点倒推）。"""
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    member = session.get(Member, ids.member_id)
    tenant = session.get(Tenant, ids.tenant_id)
    if member is None or tenant is None:
        return JSONResponse(status_code=200, content=ok({"updated": False}))

    fields = payload.model_fields_set
    if "accounting_mode" in fields and payload.accounting_mode is not None:
        if (ids.role or "").strip().lower() not in ("boss", "manager"):
            require_roles(context, BOSS_ONLY)
        tenant.accounting_mode = payload.accounting_mode
    if "auto_post_enabled" in fields and payload.auto_post_enabled is not None:
        member.auto_post_enabled = bool(payload.auto_post_enabled)
    if "display_name" in fields and payload.display_name is not None:
        cleaned = payload.display_name.strip()
        if cleaned:
            member.name = cleaned[:32]
    session.flush()

    AuditService(session).record(
        tenant_id=ids.tenant_id,
        action="member.preferences",
        entity="member",
        entity_id=str(ids.member_id),
        member_id=ids.member_id,
        detail={
            "auto_post_enabled": bool(member.auto_post_enabled),
            "accounting_mode": str(tenant.accounting_mode),
        },
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "display_name": str(member.name),
                "accounting_mode": str(tenant.accounting_mode),
                "auto_post_enabled": bool(member.auto_post_enabled),
                "novice": novice_status_of(member),
            }
        ),
    )
