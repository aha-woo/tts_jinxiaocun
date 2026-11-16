"""权益与虚拟支付：`/entitlement/*`（5 个端点）。

**与 38 个业务 intent 完全无关**：不产生 intent、不进 ``voice_session`` 状态机、
不生成确认卡片、不写 ``usage_ledger``。这条隔离由
``tests/test_entitlement_separation.py`` 钉住。

| 端点 | 作用 |
|---|---|
| `POST /entitlement/order` | 下单（幂等键派生 ``out_trade_no``） |
| `POST /entitlement/deliver-callback` | 发货推送（**唯一发货路径**，验签 + 幂等） |
| `GET /entitlement` | 当前权益（**30 天滚动窗口**） |
| `GET /entitlement/goods` | 道具列表（价格读 ``goods.json``） |
| `POST /entitlement/query-order` | ``query_order`` 查单兜底（同一幂等路径） |
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.config import Settings
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.pay.deliver_callback import DELIVER_CHANNEL_CLOUD_FUNCTION
from app.pay.goods_item import GoodsCatalog
from app.pay.sign import build_verifier
from app.pay.virtual_payment import VirtualPaymentService
from app.services.entitlement import EntitlementService

router = APIRouter(prefix="/entitlement", tags=["entitlement"])


class OrderRequest(BaseModel):
    """下单入参（**不传 tenant_id / 不传价格**）。"""

    model_config = ConfigDict(extra="forbid")

    goods_id: str = Field(min_length=1, max_length=64)


class QueryOrderRequest(BaseModel):
    """查单入参。"""

    model_config = ConfigDict(extra="forbid")

    out_trade_no: str = Field(min_length=1, max_length=64)
    #: 平台侧查单结论（真实实现由服务端调 query_order 接口获取，需核实）
    paid: bool | None = Field(default=None)


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
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    service = VirtualPaymentService(
        session,
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


@router.post("/deliver-callback", summary="发货推送（唯一发货路径）")
async def deliver_callback(
    request: Request,
    session: TenantSessionDep,
    settings: SettingsDep,
    x_wx_channel: str | None = Header(default=None, alias="X-Wx-Channel"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> JSONResponse:
    """``xpay_*`` 推送：验签 → 幂等 → 发额度（**绝不写 usage_ledger**）。

    云托管消息推送**不是**发货路径（``X-Wx-Channel: cloudrun`` 会被拒）。
    """
    body: Mapping[str, Any] = await request.json()
    channel = (x_wx_channel or DELIVER_CHANNEL_CLOUD_FUNCTION).strip().lower()
    service = VirtualPaymentService(
        session,
        verifier=_verifier_or_none(settings),
        settings=settings,
        env=int(settings.virtual_pay_env),
    )
    result = service.handle_deliver_push(
        body, tenant_id=x_tenant_id or "", channel=channel
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "delivered": result.delivered,
                "reason": result.reason,
                "error_code": result.error_code,
                "grant_quota": result.grant_quota,
                "extend_days": result.extend_days,
                "out_trade_no": result.out_trade_no,
                "wrote_usage_ledger": result.wrote_usage_ledger,
                "detail": result.detail,
            }
        ),
    )


@router.post("/query-order", summary="query_order 查单兜底（同一幂等路径）")
def query_order(
    payload: QueryOrderRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """推送丢失时补发货；重复调用也只发一次（``deliver_event`` 幂等）。"""
    ids = ids_of(context)
    service = VirtualPaymentService(session, env=int(settings.virtual_pay_env))
    result = service.query_order(
        out_trade_no=payload.out_trade_no,
        paid=payload.paid,
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
