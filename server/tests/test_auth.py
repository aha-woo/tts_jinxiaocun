"""登录链路：``wx.login code`` -> openid -> tenant_id -> JWT（T02 验收）。

覆盖
----
- 首次登录**自动开通**（租户 + 主仓 + 老板角色 + 免费权益 + 当月额度），
  重复登录回到同一租户（不是每次都新建）；
- JWT 五要素齐全（tenant_id / store_id / member_id / role / tenant_version）；
- ``tenant_version`` 变更后**旧 token 立即失效**（禁用/降级的落地手段）；
- 租户被冻结 -> ``E_TENANT_DENIED``(403)；无/坏/过期 token -> ``E_AUTH_INVALID``(401)；
- **不新增错误码**：错误码总数仍是 25 条（T01 已冻结），只在既有码里复用；
- provider 可注入：``FakeWxProvider`` 仅本地/测试，``RealWxProvider`` 生产用。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings, get_contracts
from app.deps import AuthContextDep
from app.main import create_app
from app.models.doc import DocHeader
from app.models.org import Member, Role, Store
from app.models.pay import Entitlement
from app.models.quota import UsageQuota
from app.models.tenant import Tenant
from app.services.auth import JWT_ISSUER, encode_token
from app.services.wx_provider import FakeWxProvider, RealWxProvider, build_wx_provider
from app.tenant.context import bind, reset
from app.utils.date_utils import now_utc

TEST_SECRET = "test-secret-for-t02-only-please-do-not-reuse"


def make_settings(**overrides: Any) -> Settings:
    """测试配置：内存 SQLite + 固定 JWT 密钥 + fake provider。"""
    defaults: dict[str, Any] = {
        "env": "test",
        "database_url": "sqlite+pysqlite:///:memory:",
        "jwt_secret": TEST_SECRET,
        "wx_provider": "fake",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def app() -> FastAPI:
    """起一个带受保护探针的测试 app（建表 + 注册 /api/v1/auth/login）。"""
    application = create_app(make_settings())
    application.state.database.create_all()

    @application.get("/_test/me", include_in_schema=False)
    def _me(context: AuthContextDep) -> dict[str, Any]:
        """受保护探针：只回显服务端解析出的租户上下文（不接受任何入参）。"""
        return {"code": 0, "data": context.as_dict(), "message": "ok"}

    return application


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, code: str = "wx-code-001") -> dict[str, Any]:
    """登录并返回包络里的 data。"""
    response = client.post("/api/v1/auth/login", json={"code": code})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def auth_header(token: str) -> dict[str, str]:
    """Authorization 头。"""
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ 登录与开通


def test_login_returns_jwt_and_identity(client: TestClient) -> None:
    """出参契约：{jwt, tenant_id, store_id, role, tenant_version} + 展示用字段。"""
    data = login(client)
    for key in ("jwt", "token_type", "expires_at", "tenant_id", "store_id", "member_id", "role",
                "tenant_version", "plan"):
        assert key in data, f"登录响应缺字段 {key}"
    assert data["token_type"] == "Bearer"
    assert data["role"] == "boss"
    assert data["plan"] == "free"
    assert data["tenant_version"] == 1
    assert data["expires_at"].endswith("Z"), "expires_at 必须是 ISO 8601 UTC"
    uuid.UUID(data["tenant_id"]), uuid.UUID(data["store_id"]), uuid.UUID(data["member_id"])


def test_first_login_provisions_tenant_store_role_and_quota(app: FastAPI, client: TestClient) -> None:
    """首次登录自动开通：1 租户 + 1 主仓 + 1 boss 角色 + 免费权益 + 当月额度。"""
    data = login(client)
    tenant_id = uuid.UUID(data["tenant_id"])

    with app.state.database.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None and tenant.status == "active" and tenant.plan_code == "free"
        stores = session.scalars(select(Store).where(Store.tenant_id == tenant_id)).all()
        assert len(stores) == 1 and stores[0].is_main is True and stores[0].name == "主仓"
        members = session.scalars(select(Member).where(Member.tenant_id == tenant_id)).all()
        assert len(members) == 1 and members[0].status == "active"
        roles = session.scalars(select(Role).where(Role.tenant_id == tenant_id)).all()
        assert len(roles) == 1 and roles[0].role_code == "boss"
        entitlements = session.scalars(
            select(Entitlement).where(Entitlement.tenant_id == tenant_id)
        ).all()
        assert len(entitlements) == 1 and entitlements[0].plan_code == "free"
        quotas = session.scalars(select(UsageQuota).where(UsageQuota.tenant_id == tenant_id)).all()
        assert len(quotas) == 1 and quotas[0].used_count == 0
        # 额度来自 goods.json（200 免费 + 100 首期赠送），不是代码里的魔法数
        free_plan = get_contracts().goods["free_plan"]
        expected = int(free_plan["quota_amount"]) + int(free_plan["first_period_bonus"])
        assert quotas[0].free_quota == expected == tenant.quota_monthly


def test_second_login_returns_same_tenant(client: TestClient) -> None:
    """同一 openid 重复登录回到同一租户，不重复开通。"""
    first = login(client)
    second = login(client)
    assert first["tenant_id"] == second["tenant_id"]
    assert first["member_id"] == second["member_id"]
    assert first["store_id"] == second["store_id"]


def test_different_code_is_different_tenant(client: TestClient) -> None:
    """不同微信身份 -> 不同租户（单商户 MVP 无「加入他人租户」入口）。"""
    first = login(client, code="wx-code-A")
    second = login(client, code="wx-code-B")
    assert first["tenant_id"] != second["tenant_id"]


def test_login_rejects_empty_code(client: TestClient) -> None:
    """空 code -> E_VALIDATION（复用既有错误码，不新增）。"""
    response = client.post("/api/v1/auth/login", json={"code": ""})
    assert response.status_code == 422
    assert response.json()["data"]["error_code"] == "E_VALIDATION"


def test_login_payload_never_accepts_tenant_id(client: TestClient) -> None:
    """入参只认 code/store_hint：伪造 tenant_id 不生效（防越权）。"""
    from app.schemas.auth import LoginRequest

    assert set(LoginRequest.model_fields) == {"code", "store_hint"}
    data = login(client)
    forged = client.post(
        "/api/v1/auth/login",
        json={"code": "wx-code-001", "tenant_id": str(uuid.uuid4())},
    ).json()["data"]
    assert forged["tenant_id"] == data["tenant_id"], "tenant_id 被入参影响了"


# --------------------------------------------------------------- 受保护接口行为


def test_protected_endpoint_needs_bearer_token(client: TestClient) -> None:
    """无 Authorization -> 401 E_AUTH_INVALID。"""
    response = client.get("/_test/me")
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH_INVALID"


def test_protected_endpoint_accepts_valid_token(client: TestClient) -> None:
    """带合法 token：服务端解析出的上下文与登录返回一致。"""
    data = login(client)
    response = client.get("/_test/me", headers=auth_header(data["jwt"]))
    assert response.status_code == 200
    context = response.json()["data"]
    assert context["tenant_id"] == data["tenant_id"]
    assert context["store_id"] == data["store_id"]
    assert context["member_id"] == data["member_id"]
    assert context["role_code"] == "boss"
    assert context["tenant_version"] == data["tenant_version"]


def test_tampered_token_is_rejected(client: TestClient) -> None:
    """签名不匹配 -> 401 E_AUTH_INVALID。"""
    data = login(client)
    tampered = data["jwt"][:-4] + "AAAA"
    response = client.get("/_test/me", headers=auth_header(tampered))
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH_INVALID"


def test_expired_token_is_rejected(client: TestClient) -> None:
    """过期 token -> 401 E_AUTH_INVALID（不是 500）。"""
    data = login(client)
    claims = {
        "iss": JWT_ISSUER,
        "sub": data["member_id"],
        "tenant_id": data["tenant_id"],
        "store_id": data["store_id"],
        "member_id": data["member_id"],
        "role": "boss",
        "tenant_version": data["tenant_version"],
        "iat": int(now_utc().timestamp()) - 7200,
        "exp": int(now_utc().timestamp()) - 3600,
    }
    expired = encode_token(make_settings(), claims)
    response = client.get("/_test/me", headers=auth_header(expired))
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH_INVALID"


def test_tenant_version_bump_invalidates_old_token(app: FastAPI, client: TestClient) -> None:
    """``tenant_version`` +1 后旧 token 立即失效（禁用/降级的唯一手段）。"""
    data = login(client)
    tenant_id = uuid.UUID(data["tenant_id"])

    with app.state.database.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.version = int(tenant.version) + 1

    response = client.get("/_test/me", headers=auth_header(data["jwt"]))
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH_INVALID"

    # 重新登录拿到新版本后即可继续使用
    fresh = login(client)
    assert fresh["tenant_version"] == data["tenant_version"] + 1
    assert client.get("/_test/me", headers=auth_header(fresh["jwt"])).status_code == 200


def test_frozen_tenant_is_denied(app: FastAPI, client: TestClient) -> None:
    """租户被冻结 -> 403 E_TENANT_DENIED（状态之外还带 tenant_status，便于端侧提示）。"""
    data = login(client)
    tenant_id = uuid.UUID(data["tenant_id"])
    with app.state.database.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.status = "frozen"

    response = client.get("/_test/me", headers=auth_header(data["jwt"]))
    assert response.status_code == 403
    body = response.json()
    assert body["data"]["error_code"] == "E_TENANT_DENIED"
    assert body["data"]["tenant_status"] == "frozen"


# --------------------------------------------------------------- 错误码冻结/一致性


def test_no_new_error_codes_added() -> None:
    """T02 不新增错误码：总数仍是 25 条（T01 冻结），登录只复用既有码。"""
    codes = get_contracts().error_codes
    assert len(codes) == 25
    for code in ("E_AUTH_INVALID", "E_TENANT_DENIED", "E_ROLE_DENIED", "E_NOT_FOUND", "E_VALIDATION"):
        assert code in codes, f"缺少既有错误码 {code}"
    assert codes["E_AUTH_INVALID"].http_status == 401
    assert codes["E_TENANT_DENIED"].http_status == 403
    assert codes["E_ROLE_DENIED"].http_status == 403


def test_error_codes_json_is_single_source_of_truth() -> None:
    """错误码只有 packages/shared 一份真相源，服务端不复制常量。"""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    raw = json.loads(
        (repo_root / "packages" / "shared" / "contracts" / "error-codes.json").read_text(encoding="utf-8")
    )
    assert set(raw["codes"]) == set(get_contracts().error_codes)


# ------------------------------------------------------------------- provider


def test_fake_provider_is_documented_as_non_production() -> None:
    """``FakeWxProvider`` 必须自曝「仅本地与测试，生产禁用」，防误上生产。"""
    doc = FakeWxProvider.__doc__ or ""
    assert "仅本地与测试" in doc
    assert "生产禁用" in doc


def test_provider_selection_by_settings() -> None:
    """``WX_PROVIDER`` 决定 provider；real 缺凭证 fail-fast（不静默降级成 fake）。"""
    assert isinstance(build_wx_provider(make_settings()), FakeWxProvider)
    with pytest.raises(ValueError):
        build_wx_provider(make_settings(wx_provider="real"))
    real = build_wx_provider(
        make_settings(wx_provider="real", wx_appid="wxappid", wx_app_secret="secret")
    )
    assert isinstance(real, RealWxProvider)
    with pytest.raises(ValueError):
        build_wx_provider(make_settings(wx_provider="something-else"))


def test_fake_provider_is_deterministic() -> None:
    """同一 code -> 同一 openid（测试可复现，且不联网）。"""
    provider = FakeWxProvider()
    assert provider.resolve_openid("abc").openid == provider.resolve_openid("abc").openid
    assert provider.resolve_openid("abc").openid != provider.resolve_openid("abd").openid


# ------------------------------------------------------------- 会话与上下文一致性


def test_session_carries_tenant_context(app: FastAPI, client: TestClient) -> None:
    """``Database.session(context=...)`` 绑定 contextvar：仓储无需再手工 use_context。"""
    data = login(client)
    from app.tenant.context import is_bound, require
    from app.tenant.repository import tenant_repository

    with app.state.database.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, uuid.UUID(data["tenant_id"]))
        assert tenant is not None
        from app.tenant.context import TenantContext

        context = TenantContext(
            tenant_id=tenant.tenant_id,
            store_id=uuid.UUID(data["store_id"]),
            member_id=uuid.UUID(data["member_id"]),
            role_code="boss",
            tenant_version=int(tenant.version),
        )

    token = bind(context)
    try:
        with app.state.database.session(context=context) as session:
            assert is_bound() is True
            assert require().tenant_id == context.tenant_id
            repo = tenant_repository(session, DocHeader)
            assert repo.count() == 0  # 新租户没有单据，且不会看到别人的
    finally:
        reset(token)
    assert is_bound() is False

    with app.state.database.session(bypass_scope=True) as session:
        assert session.scalar(select(func.count()).select_from(DocHeader)) == 0
