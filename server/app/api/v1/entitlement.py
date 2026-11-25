"""权益与虚拟支付：`/entitlement/*`（5 个端点）。

**与 38 个业务 intent 完全无关**：不产生 intent、不进 ``voice_session`` 状态机、
不生成确认卡片、不写 ``usage_ledger``。这条隔离由
``tests/test_entitlement_separation.py`` 钉住。

| 端点 | 作用 |
|---|---|
| `POST /entitlement/order` | 下单（幂等键派生 ``outTradeNo``） |
| `POST /entitlement/deliver-callback` | 发货推送（**唯一发货路径**；服务间鉴权 + 幂等） |
| `GET /entitlement` | 当前权益（**30 天滚动窗口**） |
| `GET /entitlement/goods` | 道具列表（价格读 ``goods.json``） |
| `POST /entitlement/query-order` | ``query_order`` 查单兜底（同一幂等路径） |

发货推送的租户从哪来（P0-3 的根因）
------------------------------------
推送体里只有 ``out_trade_no``，没有用户 JWT；而所有业务写入都受 RLS
``tenant_id = current_setting('app.tenant_id')`` 约束。做法与「登录鸡生蛋」同构：

1. 服务间令牌 + 通道校验（``ServiceDeliverDep``，与用户态鉴权链无关）；
2. 开 ``app.deliver_bootstrap`` → 按 ``out_trade_no`` 精确查 ``virtual_order`` 拿 ``tenant_id``
   （只读、只在「无租户上下文 + 开关 on」时可见，策略名 ``xpay_deliver_lookup``）；
3. **立刻** ``set_tenant_guc(tenant_id)`` + 关开关 → 交业务（业务阶段全程走正常 RLS）。

全程没有 ``bypass_scope``：发货路径上不允许出现「关掉应用层 scope」的后门。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.v1._shared import BOSS_ONLY, assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import failure, ok
from app.common.errors import FALLBACK_ERROR_CODE, AppError
from app.config import Settings
from app.deps import (
    AuthContextDep,
    ContractsDep,
    DatabaseDep,
    ServiceDeliverDep,
    SettingsDep,
    TenantSessionDep,
)
from app.models.pay import VirtualOrder
from app.models.tenant import Tenant
from app.pay.deliver_callback import DELIVER_CHANNEL_CLOUD_FUNCTION, DeliverResult
from app.pay.goods_item import GoodsCatalog
from app.pay.sign import build_verifier
from app.pay.virtual_payment import VirtualPaymentService
from app.services.entitlement import EntitlementService
from app.tenant import rls
from app.tenant.context import TenantContext, bind, reset

router = APIRouter(prefix="/entitlement", tags=["entitlement"])

#: 发货链路不碰门店/成员维度的数据（只写租户级的 quota / entitlement / deliver_event），
#: 用 nil UUID 比「随手塞一个真实 id」更诚实。
NIL_UUID = uuid.UUID(int=0)

#: 发货链路的角色标记（**不是**某个真实用户的 role，见 ServiceCaller 的说明）
SERVICE_ROLE_CODE = "system"


class OrderRequest(BaseModel):
    """下单入参（**不传 tenant_id / 不传价格**）。"""

    model_config = ConfigDict(extra="forbid")

    goods_id: str = Field(min_length=1, max_length=64)


class QueryOrderRequest(BaseModel):
    """查单入参（不得接受客户端传入 ``paid`` 状态）。"""

    model_config = ConfigDict(extra="forbid")

    out_trade_no: str = Field(min_length=1, max_length=64)


@router.get("/goods", summary="道具列表（价格来自 goods.json）")
def list_goods(context: AuthContextDep) -> JSONResponse:
    """上架道具；**不得引导外部付款**。"""
    del context
    catalog = GoodsCatalog()
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [goods.as_dict() for goods in catalog.all(on_shelf_only=True)],
                "channel": catalog.channel(),
                "forbidden_channels": list(catalog.forbidden_channels()),
                "min_payment_fen": catalog.min_payment_fen(),
                "auto_renew": False,
                "note": "一次性道具直购；到期不自动扣费，靠自有推送提醒复购",
            }
        ),
    )


@router.get("", summary="当前权益（30 天滚动窗口口径）")
def get_entitlement(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """权益与用量**分两个子对象**返回，禁止拼成一个进度条。"""
    ids = ids_of(context)
    service = EntitlementService(session)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "entitlement": service.current(tenant_id=ids.tenant_id).as_dict(),
                "usage": service.usage(tenant_id=ids.tenant_id).as_dict(),
                "caliber_note": (
                    "entitlement 是 30 天滚动窗口，usage 是自然月 YYYY-MM；"
                    "两者分别存储、不得互相推导"
                ),
            }
        ),
    )


@router.post("/order", summary="下单（幂等键派生 outTradeNo）")
def create_order(
    payload: OrderRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """生成订单 + ``wx.requestVirtualPayment`` 参数；**前端 success 不发货**。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    service = VirtualPaymentService(
        session,
        settings=settings,
        env=int(settings.virtual_pay_env),
    )
    order = service.create_order(
        tenant_id=ids.tenant_id,
        member_id=ids.member_id,
        goods_id=payload.goods_id,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "order": order.as_dict(),
                "deliver_rule": "发货只以发货推送为准；前端 success 不作发货依据",
            }
        ),
    )


# --------------------------------------------------------------------------- 发货推送


def _out_trade_no_of(body: Mapping[str, Any]) -> str:
    """取 ``OutTradeNo``（兼容两种写法）。"""
    return str(body.get("OutTradeNo") or body.get("out_trade_no") or "").strip()


def _pick_tenant(
    orders: Sequence[VirtualOrder],
    *,
    hint: uuid.UUID | None,
    out_trade_no: str,
) -> uuid.UUID:
    """从反查结果里定出**唯一**租户；定不出来就拒绝，绝不猜。

    订单表是 ``UNIQUE(tenant_id, out_trade_no)``，单号本身**不全球唯一**；
    引导策略又只放开了 SELECT，所以理论上可能查到多行。此时必须由云函数带
    ``X-Tenant-Id`` 消歧（它在推送上下文里拿得到租户）。
    """
    tenant_ids = {order.tenant_id for order in orders}
    if len(tenant_ids) == 1:
        return next(iter(tenant_ids))
    if not tenant_ids:
        scope = f"（租户 {hint}）" if hint is not None else ""
        raise AppError(
            "E_NOT_FOUND",
            message=f"发货推送的订单 {out_trade_no} 不存在{scope}，拒绝发放额度",
            data={
                "reason": "order_not_found",
                "out_trade_no": out_trade_no,
                "tenant_hint": str(hint) if hint is not None else None,
            },
        )
    raise AppError(
        "E_VALIDATION",
        message=(
            f"单号 {out_trade_no} 在 {len(tenant_ids)} 个租户下都存在，无法判定归属；"
            "请由云函数带上 X-Tenant-Id"
        ),
        data={
            "reason": "out_trade_no_ambiguous",
            "out_trade_no": out_trade_no,
            "tenant_count": len(tenant_ids),
        },
    )


def _resolve_tenant_context(
    database: Any,  # noqa: ANN401 - Database（避免与应用层循环导入）
    *,
    out_trade_no: str,
    tenant_hint: uuid.UUID | None,
) -> TenantContext:
    """用 ``out_trade_no`` 反查租户，并按「开引导 → 查 → 立刻切回租户模式」执行。

    这个函数是**同步**的，由端点丢进线程池执行（里面全是阻塞 DB 调用）。

    Args:
        database: 数据库句柄。
        out_trade_no: 推送里的商户订单号。
        tenant_hint: 云函数带来的租户提示（可空）。

    Returns:
        已绑定好 ``tenant_id / tenant_version`` 的租户上下文。

    Raises:
        AppError: 缺单号 / 订单不存在 / 单号跨租户歧义。
    """
    if not out_trade_no:
        raise AppError(
            "E_VALIDATION",
            message="推送体缺少 OutTradeNo，无法定位订单与租户",
            data={"reason": "missing_out_trade_no"},
        )

    resolved: uuid.UUID | None = None
    tenant_version = 1
    # 无租户上下文开会话：PG 上 app.tenant_id 为空，正是引导策略要求的条件
    with database.session() as session:
        rls.set_deliver_bootstrap(session, True)
        try:
            statement = select(VirtualOrder).where(VirtualOrder.out_trade_no == out_trade_no)
            if tenant_hint is not None:
                statement = statement.where(VirtualOrder.tenant_id == tenant_hint)
            orders = list(session.execute(statement).scalars())
            resolved = _pick_tenant(orders, hint=tenant_hint, out_trade_no=out_trade_no)
        finally:
            # 无论成功还是抛错，都先关开关；拿到租户就立刻切回租户模式。
            # 之后的任何查询都只看得见本租户的行（不借引导开关做业务读）。
            rls.set_deliver_bootstrap(session, False)
            if resolved is not None:
                rls.set_tenant_guc(session, resolved)

        if resolved is not None:
            tenant = session.get(Tenant, resolved)
            if tenant is None:
                raise AppError(
                    "E_TENANT_DENIED",
                    message="订单指向的租户不存在，拒绝发放额度",
                    data={"reason": "tenant_not_found", "tenant_id": str(resolved)},
                )
            tenant_version = int(tenant.version)

    if resolved is None:  # pragma: no cover - _pick_tenant 不会返回 None
        raise AppError("E_INTERNAL", message="租户反查未产生结果")

    return TenantContext(
        tenant_id=resolved,
        store_id=NIL_UUID,
        member_id=NIL_UUID,
        role_code=SERVICE_ROLE_CODE,
        tenant_version=tenant_version,
    )


def _run_deliver(
    database: Any,  # noqa: ANN401 - Database
    settings: Settings,
    body: Mapping[str, Any],
    context: TenantContext,
) -> DeliverResult:
    """在已确定的租户上下文里执行「验签 → 幂等 → 发额度」（同步，跑在线程池）。"""
    token = bind(context)
    try:
        with database.session(context=context) as session:
            service = VirtualPaymentService(
                session,
                verifier=_verifier_or_none(settings),
                settings=settings,
                env=int(settings.virtual_pay_env),
            )
            return service.handle_deliver_push(
                body,
                tenant_id=str(context.tenant_id),
                channel=DELIVER_CHANNEL_CLOUD_FUNCTION,
            )
    finally:
        reset(token)


@router.post("/deliver-callback", summary="发货推送（唯一发货路径）")
async def deliver_callback(
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    contracts: ContractsDep,
    caller: ServiceDeliverDep,
) -> JSONResponse:
    """``xpay_*`` 推送：**服务间鉴权** → 反查租户 → 幂等 → 发额度（绝不写 ``usage_ledger``）。

    鉴权与租户解析都不在用户态鉴权链上（``ServiceDeliverDep`` 独立），
    所以这条路径**不需要**、也**不接受**用户 JWT。云托管消息推送不是发货路径
    （``X-Wx-Channel: cloudrun`` 会被拒）。

    响应口径（云函数按它决定「要不要让微信重推」）
    ----------------------------------------------
    - ``delivered=True`` → ``code=0``（HTTP 200）；
    - 重复推送 → ``code=1402``（``E_DELIVER_DUPLICATE``，HTTP 200）：**幂等成功**，
      额度不重复发；
    - 其它失败（验签失败 / 道具不存在 / 环境不允许）→ 按契约的错误码与 HTTP 状态返回
      （401/422…），**绝不伪装成 200 成功**，否则云函数会把「没发货」当「已发货」。
    """
    raw = await request.json()
    if not isinstance(raw, Mapping):
        raise AppError(
            "E_VALIDATION",
            message="发货推送体必须是 JSON 对象",
            data={"reason": "body_not_object"},
        )
    body: Mapping[str, Any] = raw
    out_trade_no = _out_trade_no_of(body)

    context = await run_in_threadpool(
        _resolve_tenant_context,
        database,
        out_trade_no=out_trade_no,
        tenant_hint=caller.tenant_hint,
    )
    result = await run_in_threadpool(_run_deliver, database, settings, body, context)

    data: dict[str, Any] = {
        "delivered": result.delivered,
        "reason": result.reason,
        "grant_quota": result.grant_quota,
        "extend_days": result.extend_days,
        "out_trade_no": result.out_trade_no or out_trade_no,
        "wrote_usage_ledger": result.wrote_usage_ledger,
        "channel": caller.channel,
        "tenant_id": str(context.tenant_id),
    }
    if result.delivered:
        return JSONResponse(status_code=200, content=ok(data))

    spec = contracts.error_codes.get(result.error_code or "") or contracts.error_codes[
        FALLBACK_ERROR_CODE
    ]
    data["error_code"] = spec.code
    return JSONResponse(
        status_code=spec.http_status,
        content=failure(spec, message=result.detail or None, data=data),
    )


@router.post("/query-order", summary="query_order 查单兜底（同一幂等路径）")
def query_order(
    payload: QueryOrderRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """推送丢失时补发货；重复调用也只发一次（``deliver_event`` 幂等）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    service = VirtualPaymentService(session, settings=settings, env=int(settings.virtual_pay_env))
    result = service.query_order(
        out_trade_no=payload.out_trade_no,
        paid=None,
        tenant_id=str(ids.tenant_id),
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "result": result.as_dict(),
                "note": "查单兜底复用同一条幂等路径：重复调用不会重复发额度",
            }
        ),
    )


def _verifier_or_none(settings: Settings) -> Any:
    """按配置取验签器；取不到返回 ``None``（由服务层 fail-closed）。"""
    try:
        return build_verifier(settings)
    except Exception:  # noqa: BLE001 - 配置问题交给服务层拒发货
        return None


__all__ = ["router"]
