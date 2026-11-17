"""FastAPI 依赖注入：配置、契约、幂等存储、数据库、租户上下文/JWT。

依赖顺序很重要：业务接口请把 ``require_auth`` 放在 ``get_tenant_session`` **之前**，
这样会话创建时上下文已绑定，PG 上才能 `SET LOCAL app.tenant_id`。
"""

from __future__ import annotations

import hmac
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, AsyncIterator, Iterator

from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.common.idempotency import HybridStore
from app.config import ContractBundle, Settings, get_contracts, get_settings
from app.db.session import Database
from app.models.tenant import Tenant
from app.pay.deliver_callback import DELIVER_CHANNEL_CLOUD_FUNCTION
from app.services.auth import decode_token
from app.tenant.context import (
    TenantContext,
    bind,
    context_from_claims,
    get as get_context,
    reset,
)

logger = logging.getLogger(__name__)


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


# --------------------------------------------------------------------------- 服务间鉴权

#: 服务间调用的通道头（云函数 → 云托管内网）
CHANNEL_HEADER = "X-Wx-Channel"

#: 服务间令牌头（与用户 JWT **完全独立**的一条凭据）
SERVICE_TOKEN_HEADER = "X-Service-Token"

#: 云函数带来的租户提示头
#:
#: 只用于「同一 ``out_trade_no`` 在不同租户下重号」时的消歧，
#: **不是授权依据**（真正的租户来源是 ``virtual_order`` 反查）。
TENANT_HINT_HEADER = "X-Tenant-Id"


@dataclass(frozen=True)
class ServiceCaller:
    """一个通过服务间鉴权的调用方（**不是用户**：没有 role / member 语义）。

    Attributes:
        channel: 归一化后的调用通道（当前只接受 ``cloudfunc``）。
        tenant_hint: 请求头带来的租户提示（可能为 ``None``；不可当授权用）。
        token_verified: 恒为 ``True``（能构造出该对象就说明令牌已校验通过）。
    """

    channel: str
    tenant_hint: uuid.UUID | None = None
    token_verified: bool = True


def _service_forbidden(message: str, *, data: object = None) -> AppError:
    """构造服务间鉴权的 403。

    为什么用 ``E_TENANT_DENIED``（1301，契约里就是 403）：本项目「无独立网页端 +
    错误码单一真相源」，不为了一个状态码新造错误码（改契约要走文档流程）。
    语义上它确实成立：调用方**不是**这个商户资源（发货额度）的受信主体。
    消息体里写清真实原因，避免运维看到默认文案误判成「用户没权限」。
    """
    return AppError("E_TENANT_DENIED", message=message, data=data)


async def require_service_deliver(
    settings: Annotated[Settings, Depends(get_app_settings)],
    x_wx_channel: Annotated[str | None, Header(alias=CHANNEL_HEADER)] = None,
    x_service_token: Annotated[str | None, Header(alias=SERVICE_TOKEN_HEADER)] = None,
    x_tenant_id: Annotated[str | None, Header(alias=TENANT_HINT_HEADER)] = None,
) -> ServiceCaller:
    """发货推送的服务间鉴权依赖（**不进用户态鉴权链**）。

    为什么不复用 :func:`require_auth`
    ----------------------------------
    ``require_auth`` 的前提是「用户 JWT → claims → 租户」。虚拟支付发货推送是
    **微信侧的服务调用**，压根没有用户 JWT；上一版把它挂在 ``TenantSessionDep``
    后面却没有鉴权依赖，于是 contextvar 永远没绑 → 每次 403 ``E_TENANT_DENIED``，
    付了钱的用户拿不到额度。

    三道判定，次序固定
    ------------------
    1. **服务端没配令牌 → 503 + ERROR 告警**（``E_INTERNAL`` 覆盖成 503）。
       绝不「没配就放行」：这是「付了钱发额度」的入口，配置缺失必须表现为
       「本功能不可用」，而不是「谁都能发额度」。用 503 而不是 401/403，
       是因为问题在**服务端**（重试有用），不是调用方凭据错了。
    2. **通道必须是云函数**（``X-Wx-Channel: cloudfunc``）→ 否则 403。
       云托管消息推送不是发货路径（两条通道都开着必然重复发货）。
    3. **令牌必须匹配**（``hmac.compare_digest`` 常量时间比较，bytes 比较以兼容任意字符）
       → 否则 403。缺令牌与错令牌返回同一个状态码与同一句话，
       不给「探测哪个头写错了」的信号。

    Args:
        settings: 应用配置（取 ``service_token``）。
        x_wx_channel: 通道头。
        x_service_token: 服务间令牌头。
        x_tenant_id: 租户提示头（可缺省，仅用于消歧）。

    Returns:
        :class:`ServiceCaller`

    Raises:
        AppError: 503（服务端未配置令牌）/ 403（通道不对、令牌不匹配）/ 422（租户提示不是 UUID）。
    """
    expected = str(getattr(settings, "service_token", "") or "").strip()
    if not expected:
        # 这条必须是 ERROR 级：配置缺失意味着「所有发货都会失败」，属于必须告警的部署事故
        logger.error(
            "发货推送被拒：SERVICE_TOKEN 未配置（云函数与云托管必须配置同一个令牌）；"
            "未配置时端点返回 503，不默认放行"
        )
        raise AppError(
            "E_INTERNAL",
            message="服务未就绪：发货回调缺少服务间令牌配置（SERVICE_TOKEN），请联系运维",
            data={"reason": "service_token_not_configured"},
            http_status=503,
        )

    channel = (x_wx_channel or "").strip().lower()
    if channel != DELIVER_CHANNEL_CLOUD_FUNCTION:
        raise _service_forbidden(
            "发货推送只接受云函数通道（X-Wx-Channel: cloudfunc）；"
            "云托管消息推送不是发货路径（两条通道并存必然重复发货）",
            data={"reason": "channel_not_allowed", "channel": channel or None},
        )

    given = (x_service_token or "").encode("utf-8")
    # bytes 比较：hmac.compare_digest 对 str 只接受 ASCII，令牌里一旦出现中文/emoji 会抛 TypeError
    if not hmac.compare_digest(given, expected.encode("utf-8")):
        raise _service_forbidden(
            "服务间令牌校验失败（X-Service-Token），拒绝发货",
            data={"reason": "service_token_mismatch"},
        )

    tenant_hint: uuid.UUID | None = None
    raw_hint = (x_tenant_id or "").strip()
    if raw_hint:
        try:
            tenant_hint = uuid.UUID(raw_hint)
        except ValueError as exc:
            raise AppError(
                "E_VALIDATION",
                message="租户提示头 X-Tenant-Id 不是合法 UUID",
                data={"reason": "tenant_hint_invalid", "tenant_hint": raw_hint},
            ) from exc

    return ServiceCaller(channel=channel, tenant_hint=tenant_hint, token_verified=True)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ContractsDep = Annotated[ContractBundle, Depends(get_contract_bundle)]
StoreDep = Annotated[HybridStore, Depends(get_store)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbSessionDep = Annotated[Session, Depends(get_db_session)]
TenantSessionDep = Annotated[Session, Depends(get_tenant_session)]
AuthContextDep = Annotated[TenantContext, Depends(require_auth)]
RequestIdDep = Annotated[str, Depends(get_request_id)]
ServiceDeliverDep = Annotated[ServiceCaller, Depends(require_service_deliver)]
