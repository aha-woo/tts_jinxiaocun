"""当前用户画像与店铺偏好：`GET /api/v1/me`、`PATCH /api/v1/me/preferences`。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
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
from app.common.errors import AppError
from app.deps import AuthContextDep, DatabaseDep, SettingsDep, TenantSessionDep
from app.models.catalog import ProductAlias
from app.models.dict import PersonalDict
from app.models.org import Member, Role
from app.models.tenant import Tenant
from app.services.audit import AuditService
from app.services.closing_summary import build_closing_summary, send_closing_summary
from app.services.novice import novice_status_of
from app.services.reconcile import reconcile_store
from app.services.staff import create_invite_code, join_by_invite, set_member_status

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

    personal_texts = list(
        session.execute(
            select(PersonalDict.raw_text).where(
                PersonalDict.tenant_id == ids.tenant_id,
                PersonalDict.member_id == ids.member_id,
            )
        ).scalars()
    )
    product_alias_texts = list(
        session.execute(
            select(ProductAlias.alias_text).where(ProductAlias.tenant_id == ids.tenant_id)
        ).scalars()
    )
    alias_count = len(
        {str(t).strip() for t in personal_texts if str(t).strip()}
        | {str(t).strip() for t in product_alias_texts if str(t).strip()}
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
                "alias_count": alias_count,
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


class StaffInviteRequest(BaseModel):
    """生成店铺邀请码。"""

    model_config = ConfigDict(extra="forbid")

    role_code: Literal["clerk", "manager", "readonly"] = "clerk"
    ttl_hours: int = Field(default=24, ge=1, le=168)


class StaffJoinRequest(BaseModel):
    """凭邀请码加入店铺。"""

    model_config = ConfigDict(extra="forbid")

    invite_code: str = Field(min_length=8, max_length=512)
    display_name: str | None = Field(default=None, max_length=32)


class StaffStatusPatch(BaseModel):
    """启用/停用成员或调角色。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"] | None = None
    role_code: Literal["boss", "manager", "clerk", "readonly"] | None = None


@router.post("/staff/invite", summary="老板生成店员邀请码")
def api_create_staff_invite(
    payload: StaffInviteRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """仅老板可生成 24 小时有效的带签名邀请码，发给店员后输入即可加入本店。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    data = create_invite_code(
        settings,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        inviter_member_id=ids.member_id,
        role_code=payload.role_code,
        ttl_hours=payload.ttl_hours,
    )
    AuditService(session).record(
        tenant_id=ids.tenant_id,
        action="member.invite",
        entity="store",
        entity_id=str(ids.store_id),
        member_id=ids.member_id,
        detail={"role_code": payload.role_code, "ttl_hours": payload.ttl_hours},
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.post("/staff/join", summary="凭邀请码加入店铺")
def api_join_staff(
    payload: StaffJoinRequest,
    context: AuthContextDep,
    database: DatabaseDep,
    settings: SettingsDep,
) -> JSONResponse:
    """用老板生成的邀请码加入对方店铺，并返回目标店铺的新登录态 JWT。"""
    ids = ids_of(context)
    data = join_by_invite(
        database,
        settings,
        current_context=context,
        current_member_id=ids.member_id,
        invite_code=payload.invite_code,
        display_name=payload.display_name,
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.patch("/staff/{member_id}", summary="老板启用/停用店员或改角色")
def api_patch_staff(
    member_id: str,
    payload: StaffStatusPatch,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """停用后旧 JWT 下一次请求立即失效（require_auth 校验 member.status）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    try:
        target_uuid = uuid.UUID(member_id)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="member_id 格式不合法") from exc
    data = set_member_status(
        session,
        tenant_id=ids.tenant_id,
        actor_member_id=ids.member_id,
        target_member_id=target_uuid,
        status=payload.status,
        role_code=payload.role_code,
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.get("/reconcile", summary="三账核对体检（只读）")
def api_reconcile_store(
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """核对库存余额、出入库流水、批次余量与待结清暂估行（只读不自动改账）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    data = reconcile_store(session, tenant_id=ids.tenant_id, store_id=ids.store_id)
    return JSONResponse(status_code=200, content=ok(data))


@router.post("/closing-summary/send", summary="生成并推送收摊总结（缺订阅模板走预览降级）")
def api_send_closing_summary(
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """汇总今日经营数 + 三账状态；若微信订阅模板未配置则返回 dry_run 本地总结。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    summary = build_closing_summary(session, tenant_id=ids.tenant_id, store_id=ids.store_id)
    member = session.get(Member, ids.member_id)
    openid = str(member.wx_openid) if member is not None else ""
    res = send_closing_summary(settings, openid=openid, summary=summary)
    return JSONResponse(status_code=200, content=ok(res))
