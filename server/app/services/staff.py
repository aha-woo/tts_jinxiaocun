"""店员邀请、扫码/输码入店与权限启停（M2/V1 补齐）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import Settings
from app.db.session import Database
from app.models.org import Member, Role, Store
from app.models.tenant import Tenant
from app.services.audit import AuditService
from app.services.auth import AuthService
from app.tenant import rls

__all__ = [
    "create_invite_code",
    "join_by_invite",
    "set_member_status",
    "verify_invite_code",
]

_INVITE_PREFIX = "DSH1."


def _sign(secret: str, body_b64: str) -> str:
    return hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).hexdigest()[:16]


def create_invite_code(
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    inviter_member_id: uuid.UUID,
    role_code: str = "clerk",
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """生成带 HMAC 签名的 24 小时一次性/限时店铺邀请码。"""
    if role_code not in ("clerk", "manager", "readonly"):
        raise AppError("E_VALIDATION", message="邀请角色只能是 clerk / manager / readonly")
    exp = int(time.time()) + max(1, ttl_hours) * 3600
    payload = {
        "t": str(tenant_id),
        "s": str(store_id),
        "r": role_code,
        "by": str(inviter_member_id),
        "exp": exp,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = _sign(settings.jwt_secret, b64)
    return {
        "invite_code": f"{_INVITE_PREFIX}{b64}.{sig}",
        "role_code": role_code,
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        "ttl_hours": ttl_hours,
    }


def verify_invite_code(settings: Settings, invite_code: str) -> dict[str, Any]:
    """校验邀请码签名与有效期。"""
    code = (invite_code or "").strip()
    if not code.startswith(_INVITE_PREFIX):
        raise AppError("E_VALIDATION", message="邀请码格式不正确（应以 DSH1. 开头）")
    rest = code[len(_INVITE_PREFIX) :]
    b64, sep, sig = rest.partition(".")
    if not sep or not b64 or not sig:
        raise AppError("E_VALIDATION", message="邀请码格式已损坏")
    expected = _sign(settings.jwt_secret, b64)
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        raise AppError("E_VALIDATION", message="邀请码校验失败，请向老板重新获取")
    padding = "=" * (-len(b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(b64 + padding).decode("utf-8"))
        exp = int(payload["exp"])
        tenant_id = uuid.UUID(payload["t"])
        store_id = uuid.UUID(payload["s"])
        role_code = str(payload.get("r") or "clerk")
    except Exception as exc:  # noqa: BLE001
        raise AppError("E_VALIDATION", message="邀请码内容不合法") from exc
    if int(time.time()) > exp:
        raise AppError("E_VALIDATION", message="这个邀请码已经过期了，请让老板重新生成一个")
    if role_code not in ("clerk", "manager", "readonly"):
        role_code = "clerk"
    return {
        "tenant_id": tenant_id,
        "store_id": store_id,
        "role_code": role_code,
    }


def join_by_invite(
    database: Database,
    settings: Settings,
    *,
    current_context: Any,
    current_member_id: uuid.UUID,
    invite_code: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    """用邀请码把当前微信用户加入到目标店铺（返回目标店铺新 JWT）。"""
    parsed = verify_invite_code(settings, invite_code)
    target_tenant_id: uuid.UUID = parsed["tenant_id"]
    target_store_id: uuid.UUID = parsed["store_id"]
    role_code: str = parsed["role_code"]

    with database.session(context=current_context) as src_session:
        current_member = src_session.get(Member, current_member_id)
        if current_member is None:
            raise AppError("E_AUTH_INVALID", message="当前登录成员不存在")
        openid = str(current_member.wx_openid)
        current_name = str(current_member.name)

    with database.session(bypass_scope=True) as session:
        rls.set_tenant_guc(session, target_tenant_id)
        tenant = session.get(Tenant, target_tenant_id)
        if tenant is None or str(tenant.status) != "active":
            raise AppError("E_TENANT_DENIED", message="目标店铺不存在或未在营业状态")
        store = session.execute(
            select(Store)
            .where(Store.tenant_id == target_tenant_id, Store.store_id == target_store_id)
            .limit(1)
        ).scalar_one_or_none()
        if store is None:
            raise AppError("E_NOT_FOUND", message="目标门店不存在")

        member = session.execute(
            select(Member)
            .where(Member.tenant_id == target_tenant_id, Member.wx_openid == openid)
            .limit(1)
        ).scalar_one_or_none()
        resolved_name = (display_name or "").strip() or (
            current_name if current_name not in ("", "老板") else "店员"
        )
        if member is None:
            member = Member(
                tenant_id=target_tenant_id,
                wx_openid=openid,
                name=resolved_name,
                status="active",
            )
            session.add(member)
            session.flush()
        else:
            member.status = "active"
            if display_name and display_name.strip():
                member.name = display_name.strip()

        role = session.execute(
            select(Role)
            .where(
                Role.tenant_id == target_tenant_id,
                Role.member_id == member.member_id,
                Role.store_id == target_store_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if role is None:
            role = Role(
                tenant_id=target_tenant_id,
                member_id=member.member_id,
                store_id=target_store_id,
                role_code=role_code,
            )
            session.add(role)
        else:
            role.role_code = role_code
        session.flush()

        AuditService(session).record(
            tenant_id=target_tenant_id,
            action="member.join",
            entity="member",
            entity_id=str(member.member_id),
            member_id=member.member_id,
            detail={"role_code": role_code, "store_id": str(target_store_id)},
        )

        auth_svc = AuthService(database, settings, None)  # type: ignore[arg-type]
        plan = auth_svc._active_plan(session, target_tenant_id)
        issued = auth_svc._issue(tenant, store, member, role, plan)
        return {
            "jwt": issued.token,
            "tenant_id": str(issued.tenant_id),
            "store_id": str(issued.store_id),
            "member_id": str(issued.member_id),
            "role": issued.role,
            "store_name": str(store.name),
            "member_name": str(member.name),
        }


def set_member_status(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    actor_member_id: uuid.UUID,
    target_member_id: uuid.UUID,
    status: str | None = None,
    role_code: str | None = None,
) -> dict[str, Any]:
    """老板启用/停用店员或调整其角色（禁止停用自己）。"""
    if target_member_id == actor_member_id and status == "disabled":
        raise AppError("E_VALIDATION", message="不能把自己停用，至少保留一位老板在职")
    member = session.execute(
        select(Member)
        .where(Member.tenant_id == tenant_id, Member.member_id == target_member_id)
        .limit(1)
    ).scalar_one_or_none()
    if member is None:
        raise AppError("E_NOT_FOUND", message="成员不存在")
    if status is not None:
        if status not in ("active", "disabled"):
            raise AppError("E_VALIDATION", message="status 只能是 active 或 disabled")
        member.status = status
    role = session.execute(
        select(Role)
        .where(Role.tenant_id == tenant_id, Role.member_id == target_member_id)
        .limit(1)
    ).scalar_one_or_none()
    if role_code is not None:
        if role_code not in ("boss", "manager", "clerk", "readonly"):
            raise AppError("E_VALIDATION", message="不支持的角色")
        if role is not None:
            role.role_code = role_code
    session.flush()

    AuditService(session).record(
        tenant_id=tenant_id,
        action="member.status",
        entity="member",
        entity_id=str(member.member_id),
        member_id=actor_member_id,
        detail={"status": str(member.status), "role": str(role.role_code) if role else "clerk"},
    )
    return {
        "member_id": str(member.member_id),
        "name": str(member.name),
        "status": str(member.status),
        "role": str(role.role_code) if role is not None else "clerk",
    }
