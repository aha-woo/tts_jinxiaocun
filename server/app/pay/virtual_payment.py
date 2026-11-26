"""虚拟支付：下单 → （平台侧支付）→ **只以发货推送为准** → ``query_order`` 查单兜底。

四条铁律（A7 / A10 / D12 / D13）
-----------------------------
1. **只走小程序虚拟支付** ``wx.requestVirtualPayment``；不得用普通商户号
   ``wx.requestPayment``、不得引导外部付款链接 / 客服转账（``goods.json`` 已列禁用通道）；
2. **发货只以推送为准**：前端 ``success`` 回调**绝不发货**，它只说明「用户那边看到成功」，
   不说明钱到了。:meth:`VirtualPaymentService.on_client_success` 显式返回「未发货」；
3. **``query_order`` 查单兜底走同一幂等路径**：复用
   :meth:`app.pay.deliver_callback.DeliverService.deliver_once`，
   不存在第二条发货代码路径（这是「重复发货」事故的根治办法）；
4. **``out_trade_no`` 是幂等键**：重复下单返回同一单，不生成新号。

本模块**不写 ``usage_ledger``**：发货只写 ``entitlement`` / ``usage_quota``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import Settings
from app.models.org import Member
from app.models.pay import VirtualOrder
from app.pay.deliver_callback import (
    DeliverPayload,
    DeliverResult,
    DeliverService,
    SignatureVerifier,
    current_period,
)
from app.pay.goods_item import GoodsCatalog, GoodsView, new_out_trade_no, out_trade_no_of
from app.pay.sign import build_verifier
from app.pay.wx_xpay_client import WxXpayClient

__all__ = [
    "ENV_PROD",
    "ENV_SANDBOX",
    "OrderView",
    "QueryOrderResult",
    "VirtualPaymentService",
]

#: 虚拟支付环境值（0=现网，1=沙箱）—— 沙箱 AppKey 不得用于生产
ENV_PROD = 0
ENV_SANDBOX = 1


@dataclass(frozen=True)
class OrderView:
    """一次下单的结果（前端拿 ``pay_params`` 去调 ``wx.requestVirtualPayment``）。"""

    out_trade_no: str
    goods_id: str
    goods_type: str
    price_fen: int
    quota_amount: int
    validity_days: int
    pay_status: str
    env: int
    pay_params: Mapping[str, Any] = field(default_factory=dict)
    idempotent: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "out_trade_no": self.out_trade_no,
            "goods_id": self.goods_id,
            "goods_type": self.goods_type,
            "price_fen": self.price_fen,
            "quota_amount": self.quota_amount,
            "validity_days": self.validity_days,
            "pay_status": self.pay_status,
            "env": self.env,
            "pay_params": dict(self.pay_params),
            "idempotent": self.idempotent,
            "note": self.note,
            "deliver_rule": "发货只以发货推送为准；前端 success 不作发货依据",
        }


@dataclass(frozen=True)
class QueryOrderResult:
    """``query_order`` 查单结果。"""

    out_trade_no: str
    pay_status: str
    delivered: bool = False
    duplicated: bool = False
    grant_quota: int = 0
    extend_days: int = 0
    error_code: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "out_trade_no": self.out_trade_no,
            "pay_status": self.pay_status,
            "delivered": self.delivered,
            "duplicated": self.duplicated,
            "grant_quota": self.grant_quota,
            "extend_days": self.extend_days,
            "error_code": self.error_code,
            "detail": self.detail,
        }


class VirtualPaymentService:
    """虚拟支付下单 / 查单 / 前端回调。"""

    def __init__(
        self,
        session: Session,
        *,
        catalog: GoodsCatalog | None = None,
        verifier: SignatureVerifier | None = None,
        settings: Settings | None = None,
        env: int = ENV_PROD,
        xpay_client: Any = None,
    ) -> None:
        self._session = session
        self._catalog = catalog or GoodsCatalog()
        self._settings = settings
        self._verifier = verifier if verifier is not None else _verifier_or_none(settings)
        self._env = int(env)
        self._xpay_client = xpay_client

    # ------------------------------------------------------------------ 下单
    def create_order(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID | None,
        goods_id: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> OrderView:
        """创建订单并返回 ``wx.requestVirtualPayment`` 参数。

        Args:
            tenant_id: 租户（**服务端从 JWT 取，前端不传**）。
            member_id: 下单人（可空：老订单补发时没有）。
            goods_id: 道具 id（必须存在于 ``goods.json`` 且上架）。
            idempotency_key: 幂等键；缺省按「租户 + 道具 + 当日」生成，避免重复下单。
            now: 注入时间。

        Returns:
            :class:`OrderView`。

        Raises:
            AppError: 道具不存在/已下架 ``E_VALIDATION``；低于最低支付额 ``E_VALIDATION``。
        """
        moment = now or datetime.now(timezone.utc)
        try:
            goods = self._catalog.require(goods_id)
        except ValueError as exc:
            raise AppError("E_VALIDATION", message=str(exc)) from exc

        if goods.price_fen < self._catalog.min_payment_fen():
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"道具价格 {goods.price_fen} 分低于虚拟支付最低支付额 "
                    f"{self._catalog.min_payment_fen()} 分（需核实平台规则）"
                ),
            )

        # 幂等：**幂等键决定 out_trade_no**（同一个键必然算出同一个单号）
        out_trade_no = (
            out_trade_no_of(tenant_id, idempotency_key)
            if idempotency_key
            else new_out_trade_no(tenant_id, stamp=moment.strftime("%Y%m%d%H%M%S"))
        )
        existing = self._find_order(out_trade_no, str(tenant_id))
        if existing is not None:
            return self._order_view_of(existing, goods, idempotent=True, note="幂等返回已有订单")

        order = VirtualOrder(
            tenant_id=tenant_id,
            out_trade_no=out_trade_no,
            goods_id=goods.goods_id,
            price_fen=goods.price_fen,
            pay_status="pending",
            env="prod" if self._env == ENV_PROD else "sandbox",
        )
        try:
            with self._session.begin_nested():
                self._session.add(order)
                self._session.flush()
        except IntegrityError:
            existing = self._find_order(out_trade_no, str(tenant_id))
            if existing is None:  # pragma: no cover
                raise
            return self._order_view_of(existing, goods, idempotent=True, note="并发重复下单")

        return self._order_view_of(order, goods, idempotent=False)

    # ------------------------------------------------------------------ 前端 success
    def on_client_success(self, *, out_trade_no: str) -> dict[str, Any]:
        """前端 ``success`` 回调处理：**明确不发货**。

        这个方法存在的意义就是「有一个地方可以明确说：这里不发货」。
        发货只能由 :meth:`handle_deliver_push` 或 :meth:`query_order` 触发。
        """
        return {
            "out_trade_no": out_trade_no,
            "delivered": False,
            "detail": (
                "前端 success 只表示用户侧看到支付成功，不代表钱到了；"
                "发货只以发货推送为准（推送丢失时由 query_order 兜底）"
            ),
            "next": "等待发货推送（xpay_goods_deliver_notify）",
        }

    # ------------------------------------------------------------------ 发货推送
    def handle_deliver_push(
        self,
        body: Mapping[str, Any],
        *,
        tenant_id: str = "",
        channel: str = "cloudfunc",
    ) -> DeliverResult:
        """处理发货推送（验签 → 幂等 → 发额度）。

        Args:
            body: 推送体。
            tenant_id: 租户（推送里可能没有，由云函数带上）。
            channel: 通道（只接受云开发云函数）。

        Returns:
            :class:`DeliverResult`。
        """
        payload = DeliverPayload.from_push(body, tenant_id=tenant_id)
        is_prod_runtime = bool(getattr(self._settings, "is_production", lambda: False)())
        allow_envs = ("prod",) if (is_prod_runtime and self._env == ENV_PROD) else ("prod", "sandbox")
        service = DeliverService(
            self._session,
            verifier=self._require_verifier(),
            allow_envs=allow_envs,
        )
        return service.handle(payload, channel=channel)

    # ------------------------------------------------------------------ 查单兜底
    def query_order(
        self,
        *,
        out_trade_no: str,
        paid: bool | None = None,
        goods_id: str | None = None,
        tenant_id: str = "",
        now: datetime | None = None,
    ) -> QueryOrderResult:
        """``query_order`` 定时查单兜底：推送丢失时补发货（**同一幂等路径**）。

        Args:
            out_trade_no: 商户订单号。
            paid: 平台侧查单结论（``True`` 已支付）。真实实现需调
                ``query_order`` 接口【需核实】；这里接受注入以便离线测试。
            goods_id: 道具 id（推送体缺省时补）。
            tenant_id: 租户。
            now: 注入时间。

        Returns:
            :class:`QueryOrderResult`。
        """
        del now
        order = self._find_order(out_trade_no, tenant_id)
        if order is None:
            return QueryOrderResult(
                out_trade_no=out_trade_no,
                pay_status="unknown",
                error_code="E_VALIDATION",
                detail="本地没有这个订单号，无法查单兜底",
            )
        status = str(order.pay_status)
        if paid is None:
            client = self._xpay_client
            if client is None and self._settings is not None:
                client = WxXpayClient(
                    appid=getattr(self._settings, "wx_appid", ""),
                    app_secret=getattr(self._settings, "wx_app_secret", ""),
                    pay_app_key=getattr(self._settings, "wx_pay_app_key", ""),
                    base_url=getattr(self._settings, "wx_api_base_url", "https://api.weixin.qq.com"),
                    timeout_seconds=getattr(self._settings, "wx_timeout_seconds", 5.0),
                )
            if client is not None and getattr(client, "configured", False):
                openid = ""
                member_id = getattr(order, "member_id", None)
                if member_id is not None:
                    member = self._session.get(Member, member_id)
                    if member is not None:
                        openid = str(member.wx_openid)
                if not openid:
                    fallback_member = self._session.execute(
                        select(Member)
                        .where(Member.tenant_id == order.tenant_id)
                        .order_by(Member.created_at.asc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if fallback_member is not None:
                        openid = str(fallback_member.wx_openid)
                wx_res = client.query_order(
                    openid=openid,
                    out_trade_no=out_trade_no,
                    env=0 if str(order.env) == "prod" else 1,
                )
                if wx_res.paid is None:
                    return QueryOrderResult(
                        out_trade_no=out_trade_no,
                        pay_status=status,
                        detail=wx_res.detail,
                    )
                paid = wx_res.paid
            else:
                return QueryOrderResult(
                    out_trade_no=out_trade_no,
                    pay_status=status,
                    detail="未配置微信虚拟支付查单凭证（WX_APPID / WX_APP_SECRET / WX_PAY_APP_KEY），暂仅查本地订单状态",
                )
        if not paid:
            return QueryOrderResult(
                out_trade_no=out_trade_no, pay_status=status, detail="平台侧仍未支付"
            )

        payload = DeliverPayload(
            out_trade_no=out_trade_no,
            goods_id=goods_id or str(order.goods_id),
            event_type="query_order",
            tenant_id=tenant_id,
            env="prod" if str(order.env) == "prod" else "sandbox",
            # 查单兜底不依赖推送签名：它由服务端主动发起，可信来源是平台应答
            signature="__query_order__",
            raw={"trusted_source": "query_order"},
        )
        is_prod_runtime = bool(getattr(self._settings, "is_production", lambda: False)())
        allow_envs = ("prod",) if (is_prod_runtime and self._env == ENV_PROD) else ("prod", "sandbox")
        service = DeliverService(
            self._session,
            verifier=_TrustedQueryOrderVerifier(),
            allow_envs=allow_envs,
        )
        result = service.deliver_once(payload)
        return QueryOrderResult(
            out_trade_no=out_trade_no,
            pay_status="paid" if result.delivered else status,
            delivered=result.delivered,
            duplicated=result.duplicated,
            grant_quota=result.grant_quota,
            extend_days=result.extend_days,
            error_code=result.error_code,
            detail=result.detail,
        )

    # ------------------------------------------------------------------ 内部
    def _require_verifier(self) -> SignatureVerifier:
        """取验签器；没有就先按配置构造，仍没有就报错（**不静默放行**）。"""
        if self._verifier is None:
            self._verifier = _verifier_or_none(self._settings)
        if self._verifier is None:
            raise AppError(
                "E_PAY_SIGN_INVALID",
                message="未配置发货验签器：无法验签就不发货（fail-closed）",
            )
        return self._verifier

    def _find_order(self, out_trade_no: str, tenant_id: str = "") -> VirtualOrder | None:
        """按订单号查本地订单。"""
        statement = select(VirtualOrder).where(VirtualOrder.out_trade_no == out_trade_no).limit(1)
        if tenant_id:
            try:
                statement = statement.where(VirtualOrder.tenant_id == uuid.UUID(tenant_id))
            except (ValueError, AttributeError):
                pass
        return self._session.execute(statement).scalar_one_or_none()

    def _order_view_of(
        self,
        order: VirtualOrder,
        goods: GoodsView,
        *,
        idempotent: bool,
        note: str = "",
    ) -> OrderView:
        """实体 → 视图（含前端支付参数）。"""
        return OrderView(
            out_trade_no=str(order.out_trade_no),
            goods_id=goods.goods_id,
            goods_type=goods.goods_type,
            price_fen=int(order.price_fen),
            quota_amount=goods.quota_amount,
            validity_days=goods.validity_days,
            pay_status=str(order.pay_status),
            env=self._env,
            pay_params=self._pay_params(goods),
            idempotent=idempotent,
            note=note,
        )

    def _pay_params(self, goods: GoodsView) -> dict[str, Any]:
        """``wx.requestVirtualPayment`` 参数（字段名与平台口径一致，**需核实**）。

        - ``env``：0 现网 / 1 沙箱；
        - ``goodsPrice``：单位「分」；
        - ``currencyType``：``CNY``；
        - ``platform``：端侧传，服务端给的默认值仅作占位。
        """
        return {
            "offerId": goods.goods_id,
            "buyQuantity": 1,
            "env": self._env,
            "currencyType": "CNY",
            "platform": "android",
            "productId": goods.goods_id,
            "goodsPrice": goods.price_fen,
            "channel": self._catalog.channel(),
            "autoRenew": False,
        }

    def period(self, now: datetime | None = None) -> str:
        """自然月口径（与发货加额度同口径）。"""
        return current_period(now)


class _TrustedQueryOrderVerifier(SignatureVerifier):
    """查单兜底的内部验签器：**只对服务端主动发起的查单结论放行**。

    它不是「验签豁免」，而是「来源不同」：``query_order`` 是服务端主动请求平台
    得到的可信应答，本来就没有推送签名可验。真正的防护是**幂等键**
    （``deliver_event.UNIQUE(tenant_id, out_trade_no)``）——即使查单兜底被重复触发，
    额度也只会发一次。
    """

    def verify(self, payload: DeliverPayload) -> bool:
        """只接受标记为 ``query_order`` 的可信来源。"""
        raw = dict(payload.raw or {})
        return raw.get("trusted_source") == "query_order" and bool(payload.out_trade_no)


def _verifier_or_none(settings: Settings | None) -> SignatureVerifier | None:
    """按配置构造验签器（没有配置返回 ``None``，由调用方 fail-closed）。"""
    if settings is None:
        return None
    try:
        return build_verifier(settings)
    except Exception:  # noqa: BLE001 - 配置错误不该在这里炸，交给调用方判
        return None
