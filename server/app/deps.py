"""FastAPI 依赖注入：配置、契约、幂等存储、数据库、租户上下文/JWT。

依赖顺序很重要：业务接口请把 ``require_auth`` 放在 ``get_tenant_session`` **之前**，
这样会话创建时上下文已绑定，PG 上才能 `SET LOCAL app.tenant_id`。
"""

from __future__ import annotations

import uuid
from typing import Annotated, AsyncIterator, Iterator

from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.common.idempotency import HybridStore
from app.config import ContractBundle, Settings, get_contracts, get_settings
from app.db.session import Database
from app.models.tenant import Tenant
from app.services.auth import decode_token
from app.tenant.context import (
    TenantContext,
    bind,
    context_from_claims,
    get as get_context,
    reset,
)


def get_app_settings(request: Request) -> Settings:
    """配置单例：优先取 ``app.state``（create_app 注入的实例），否则用进程级默认。

    这样测试用 ``create_app(Settings(env="test", ...))`` 注入的配置才真正生效
    （否则 ``/readyz`` 与登录会读到进程级默认值，测出来的东西不是被测对象）。
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return settings
    return get_settings()


def get_contract_bundle(request: Request) -> ContractBundle:
    """共享契约单例：优先取 app.state（create_app 已强校验），否则用进程级默认契约。"""
    bundle = getattr(request.app.state, "contract_bundle", None)
    if bundle is not None:
        return bundle
    return get_contracts()


def get_store(request: Request) -> HybridStore:
    """从 ``app.state`` 取幂等存储实例。"""
    store = getattr(request.app.state, "idempotency_store", None)
    if store is None:  # pragma: no cover - 仅在 create_app 未初始化时触发
        raise RuntimeError("idempotency_store 未初始化，请检查 create_app 生命周期")
    return store


def get_database(request: Request) -> Database:
    """从 ``app.state`` 取数据库句柄（engine + session factory）。"""
    database = getattr(request.app.state, "database", None)
    if database is None:  # pragma: no cover
        raise RuntimeError("database 未初始化，请检查 create_app 生命周期")
    return database


def get_db_session(database: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    """无租户上下文的会话（仅登录引导 / 平台级只读使用）。"""
    with database.session() as session:
        yield session


def get_tenant_session(database: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    """带租户上下文的会话：PG 上自动 `SET LOCAL app.tenant_id`（事务级，不泄漏）。"""
    context = get_context()
    if context is None:
        raise AppError("E_TENANT_DENIED", message="缺少租户上下文，禁止访问业务数据")
    with database.session(context=context) as session:
        yield session


def _extract_bearer(authorization: str | None) -> str:
    """从 Authorization 头取出 Bearer token。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("E_AUTH_INVALID", message="缺少登录凭证（Authorization: Bearer <jwt>）")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise AppError("E_AUTH_INVALID", message="登录凭证为空")
    return token


async def require_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    database: Annotated[Database, Depends(get_database)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AsyncIterator[TenantContext]:
    """校验 JWT、核对租户状态与版本，并在本次请求内绑定租户上下文。

    - 签名/过期问题 → ``E_AUTH_INVALID``(401)
    - 租户被冻结/销户中 → ``E_TENANT_DENIED``(403)
    - `tenant_version` 不一致（租户被禁用后旧 token）→ ``E_AUTH_INVALID``(401)

    **为什么是 async 生成器依赖**：contextvar 的写入必须在**请求所在的 task** 里发生，
    后续依赖（``get_tenant_session``）与业务代码才看得见。同步生成器依赖会被 Starlette
    放进线程池执行，写入落在「上下文副本」里，出了依赖就丢了（表现为莫名
    ``E_TENANT_DENIED``）。因此这里做成 async，只把 **阻塞的 DB 查询**丢进线程池。
    """
    bearer = _extract_bearer(authorization)
    claims = decode_token(settings, bearer)
    try:
        context = context_from_claims(claims)
    except ValueError as exc:
        raise AppError("E_AUTH_INVALID", message="登录凭证缺少租户信息") from exc

    def _load_tenant() -> tuple[int, str, str]:
        """线程池里做的阻塞查询：取 tenant 版本 / 状态 / 档位。"""
        with database.session(context=context) as session:
            tenant = session.get(Tenant, context.tenant_id)
            if tenant is None:
                raise AppError("E_TENANT_DENIED", message="租户不存在")
            return int(tenant.version), str(tenant.status), str(tenant.plan_code)

    tenant_version, tenant_status, tenant_plan = await run_in_threadpool(_load_tenant)

    if tenant_version != context.tenant_version:
        raise AppError("E_AUTH_INVALID", message="登录状态已失效，请重新进入小程序")
    if tenant_status != "active":
        raise AppError(
            "E_TENANT_DENIED",
            message=f"租户状态为 {tenant_status}，暂不可用",
            data={"tenant_status": tenant_status},
        )
    # 补充 plan（展示用；权限判定仍以 role/tenant_status 为准）
    request.state.tenant_plan = tenant_plan

    context_token = bind(context)
    try:
        yield context
    finally:
        # 带上 X-Request-Id：万一真的发生了跨 context 复位，告警日志才能定位到请求
        reset(context_token, request_id=request.headers.get("x-request-id", ""))


def get_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> str:
    """获取/生成请求 id（日志串联与成本归因用）。"""
    if x_request_id is not None and x_request_id.strip() != "":
        return x_request_id.strip()
    return uuid.uuid4().hex


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ContractsDep = Annotated[ContractBundle, Depends(get_contract_bundle)]
StoreDep = Annotated[HybridStore, Depends(get_store)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[Session, Depends(get_db_session)]
TenantSessionDep = Annotated[Session, Depends(get_tenant_session)]
AuthContextDep = Annotated[TenantContext, Depends(require_auth)]
RequestIdDep = Annotated[str, Depends(get_request_id)]
