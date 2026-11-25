"""AuthService：`wx.login` code -> openid -> tenant_id -> JWT（架构 §4.2 登录链路）。

要点
----
1. **不做账号密码**（D15）：身份来自微信 openid；
2. 首次登录自动开通租户（1 租户 + 1 主仓 + 1 boss + 免费权益 + 当月额度），保证「说一句就能记账」；
3. JWT 带 `tenant_id / store_id / member_id / role / tenant_version`：
   `tenant_version` 变化（禁用/降级）后**旧 token 立即失效**；
4. 反查 openid 属于「RLS 鸡生蛋」场景：走 ``bypass_scope=True`` + PG 上的
   `app.auth_bootstrap` 会话标志（只放开 member 的 SELECT，见 tenant/rls.py）；
5. 免费额度来自 `goods.json` 的 `free_plan`（单一真相源），**不在代码里硬编码价格/额度**。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import Settings, get_contracts
from app.db.session import Database
from app.models.org import Member, Role, Store
from app.models.pay import Entitlement
from app.models.quota import UsageQuota
from app.models.tenant import Tenant
from app.services.wx_provider import WxIdentity, WxProvider
from app.tenant import rls
from app.utils.date_utils import now_utc, today_in_tz

#: 默认免费额度（从 goods.json 读取，这里仅作兜底常量；需核实）
FALLBACK_FREE_QUOTA = 200

JWT_ISSUER = "voice-inventory-lite"


@dataclass(frozen=True)
class LoginResult:
    """登录结果（路由层直接映射为 LoginResponse）。"""

    token: str
    tenant_id: uuid.UUID
    store_id: uuid.UUID
    member_id: uuid.UUID
    role: str
    tenant_version: int
    plan: str
    expires_at: datetime


def encode_token(settings: Settings, claims: dict[str, Any]) -> str:
    """签发 HS256 JWT（payload 必须含 exp/iat/tenant 五要素）。"""
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(settings: Settings, token: str) -> dict[str, Any]:
    """校验并解出 claims。

    Raises:
        AppError: ``E_AUTH_INVALID``（签名错 / 过期 / 结构不全）
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "tenant_id", "store_id", "member_id", "role", "tenant_version"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError("E_AUTH_INVALID", message="登录已过期，请重新进入小程序") from exc
    except jwt.InvalidTokenError as exc:
        raise AppError("E_AUTH_INVALID", message="登录凭证无效") from exc
    return dict(claims)


def free_plan_quota() -> int:
    """免费档额度（`goods.json` -> free_plan.quota_amount + first_period_bonus）。"""
    goods = get_contracts().goods
    free_plan = goods.get("free_plan") or {}
    quota = int(free_plan.get("quota_amount", FALLBACK_FREE_QUOTA))
    bonus = int(free_plan.get("first_period_bonus", 0))
    return quota + bonus


class AuthService:
    """登录与租户开通。"""

    def __init__(self, database: Database, settings: Settings, provider: WxProvider) -> None:
        self._db = database
        self._settings = settings
        self._provider = provider

    # ------------------------------------------------------------------ login
    def login(self, code: str, store_hint: str | None = None) -> LoginResult:
        """用 wx.login code 登录；不存在则自动开通租户。"""
        identity = self._provider.resolve_openid(code)
        with self._db.session(bypass_scope=True) as session:
            # PG：先开登录引导标志，才能在没有 tenant 上下文时反查 member
            rls.set_auth_bootstrap(session, True)
            member = self._find_member_by_openid(session, identity.openid)
            if member is None:
                try:
                    with session.begin_nested():
                        tenant, store, member, role = self._provision(session, identity)
                except Exception:
                    rls.set_auth_bootstrap(session, True)
                    member = self._find_member_by_openid(session, identity.openid)
                    if member is None:
                        raise
                    if str(member.status) != "active":
                        raise AppError("E_TENANT_DENIED", message="账号已停用，请联系老板")
                    rls.set_tenant_guc(session, member.tenant_id)
                    rls.set_auth_bootstrap(session, False)
                    tenant = session.get(Tenant, member.tenant_id)
                    if tenant is None:
                        raise AppError("E_TENANT_DENIED", message="租户不存在")
                    store = self._resolve_store(session, tenant.tenant_id, store_hint)
                    role = self._resolve_role(session, tenant.tenant_id, member.member_id, store.store_id)
            else:
                if str(member.status) != "active":
                    raise AppError("E_TENANT_DENIED", message="账号已停用，请联系老板")
                # 拿到 tenant_id 后立即绑 GUC，关闭引导标志（后续查询走正常 RLS）
                rls.set_tenant_guc(session, member.tenant_id)
                rls.set_auth_bootstrap(session, False)
                tenant = session.get(Tenant, member.tenant_id)
                if tenant is None:  # pragma: no cover - 外键保证
                    raise AppError("E_TENANT_DENIED", message="租户不存在")
                store = self._resolve_store(session, tenant.tenant_id, store_hint)
                role = self._resolve_role(session, tenant.tenant_id, member.member_id, store.store_id)
            plan = self._active_plan(session, tenant.tenant_id)
            return self._issue(tenant, store, member, role, plan)

    # ------------------------------------------------------------- internals
    def _find_member_by_openid(self, session: Session, openid: str) -> Member | None:
        """按 openid 反查员工（一人多店时取最早创建，MVP 只暴露单商户）。"""
        statement = select(Member).where(Member.wx_openid == openid).order_by(Member.created_at.asc()).limit(1)
        return session.scalars(statement).first()

    def _resolve_store(self, session: Session, tenant_id: uuid.UUID, store_hint: str | None) -> Store:
        """定位门店：hint 优先，其次主仓，最后任意一个。"""
        if store_hint:
            hinted = session.scalars(
                select(Store).where(Store.tenant_id == tenant_id, Store.name == store_hint).limit(1)
            ).first()
            if hinted is not None:
                return hinted
        main_store = session.scalars(
            select(Store).where(Store.tenant_id == tenant_id, Store.is_main.is_(True)).limit(1)
        ).first()
        if main_store is not None:
            return main_store
        any_store = session.scalars(select(Store).where(Store.tenant_id == tenant_id).limit(1)).first()
        if any_store is None:  # pragma: no cover - 开通时必建主仓
            raise AppError("E_TENANT_DENIED", message="租户没有可用门店")
        return any_store

    def _resolve_role(
        self, session: Session, tenant_id: uuid.UUID, member_id: uuid.UUID, store_id: uuid.UUID
    ) -> Role:
        """取员工在该门店的角色（门店级优先于全店级）。"""
        statements = (
            select(Role).where(
                Role.tenant_id == tenant_id, Role.member_id == member_id, Role.store_id == store_id
            ),
            select(Role).where(
                Role.tenant_id == tenant_id, Role.member_id == member_id, Role.store_id.is_(None)
            ),
        )
        for statement in statements:
            role = session.scalars(statement.limit(1)).first()
            if role is not None:
                return role
        raise AppError("E_ROLE_DENIED", message="该员工没有可用角色")

    def _provision(
        self, session: Session, identity: WxIdentity
    ) -> tuple[Tenant, Store, Member, Role]:
        """首次登录开通：租户 + 主仓 + 老板 + 免费权益 + 当月额度。

        **开通过程也要过 RLS**：``tenant`` 表本身同样启用了
        ``FORCE ROW LEVEL SECURITY``，其 ``WITH CHECK`` 要求
        ``tenant_id = current_setting('app.tenant_id')``。因此这里先确定 tenant_id、
        把 GUC 指向它，再写入任何行；否则（GUC 未设 → NULL 比较恒为 false）INSERT 会被拒绝。
        """
        now = now_utc()
        tenant_id = uuid.uuid4()
        # PG：先绑 GUC 再写行（SQLite 上是无操作，靠应用层 scope 保证）
        rls.set_tenant_guc(session, tenant_id)
        # 开通期间不再需要跨租户反查权限，立即关掉登录引导标志
        rls.set_auth_bootstrap(session, False)

        tenant = Tenant(
            tenant_id=tenant_id,
            name="我的店",
            plan_code="free",
            status="active",
            quota_monthly=free_plan_quota(),
            version=1,
        )
        session.add(tenant)
        # **必须显式 flush**：SQLAlchemy 的 unit-of-work 在没有 relationship() 时
        # 不保证按外键顺序发 INSERT。真 PG 上实测过：一次性 add 六个对象再 flush，
        # 它先插 member，于是 fk_member_tenant_id_tenant 报
        # "Key is not present in table tenant"。SQLite 默认不校验外键，所以本地测试
        # 永远发现不了。这里按依赖顺序逐段落库（tenant → store → member → 其余），
        # 与是否有 relationship 无关。
        session.flush()

        store = Store(
            store_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            name="主仓",
            is_main=True,
            status="active",
        )
        session.add(store)

        member = Member(
            member_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            wx_openid=identity.openid,
            wx_unionid=identity.unionid,
            name="老板",
            status="active",
        )
        session.add(member)
        session.flush()  # member 先于 role（role.member_id 无 FK，但与 member 同批更安全）

        role = Role(
            role_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            member_id=member.member_id,
            store_id=None,
            role_code="boss",
        )
        session.add(role)

        quota = free_plan_quota()
        free_plan = get_contracts().goods.get("free_plan") or {}
        validity_days = int(free_plan.get("validity_days", 30))
        session.add(
            Entitlement(
                entitlement_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                plan_code="free",
                valid_from=now,
                valid_until=now + timedelta(days=validity_days),
                quota_granted=quota,
                remaining=quota,
                degrade_level="L0",
                status="active",
            )
        )
        session.add(
            UsageQuota(
                quota_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                period=today_in_tz()[:7],
                free_quota=quota,
                used_count=0,
                degrade_level="L0",
            )
        )
        session.flush()
        return tenant, store, member, role

    def _active_plan(self, session: Session, tenant_id: uuid.UUID) -> str:
        """当前生效权益档位（过期则回落到 free）。"""
        statement = (
            select(Entitlement)
            .where(Entitlement.tenant_id == tenant_id, Entitlement.status == "active")
            .order_by(Entitlement.valid_until.desc())
            .limit(1)
        )
        entitlement = session.scalars(statement).first()
        if entitlement is None:
            return "free"
        valid_until = entitlement.valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        return entitlement.plan_code if valid_until > now_utc() else "free"

    def _issue(self, tenant: Tenant, store: Store, member: Member, role: Role, plan: str) -> LoginResult:
        """签发 JWT（含 tenant_version 用于旧 token 立即失效）。"""
        issued_at = now_utc()
        expires_at = issued_at + timedelta(seconds=self._settings.jwt_expire_seconds)
        claims: dict[str, Any] = {
            "iss": JWT_ISSUER,
            "sub": str(member.member_id),
            "tenant_id": str(tenant.tenant_id),
            "store_id": str(store.store_id),
            "member_id": str(member.member_id),
            "role": role.role_code,
            "tenant_version": int(tenant.version),
            "plan": plan,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        return LoginResult(
            token=encode_token(self._settings, claims),
            tenant_id=tenant.tenant_id,
            store_id=store.store_id,
            member_id=member.member_id,
            role=role.role_code,
            tenant_version=int(tenant.version),
            plan=plan,
            expires_at=expires_at,
        )
