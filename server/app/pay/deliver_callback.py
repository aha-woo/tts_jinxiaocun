"""虚拟支付发货回调：**单路径 + 验签 + OutTradeNo 幂等 + 只发额度**。

单路径（团队已拍）
------------------
虚拟支付发货**只认一条通道**：
``云开发云函数消息推送``（``xpay_goods_deliver_notify`` 等 ``xpay_*`` 事件）。
``云托管消息推送`` **不作为**发货路径（两条通道同时开着必然重复发货，
且云托管侧拿不到与云函数一致的签名上下文）。

四道次序，一个都不能颠倒
------------------------
1. **先验签**：失败 → ``E_PAY_SIGN_INVALID``(1401)，**不写 ``deliver_event``、不发额度**
   （连「失败记录」都不落，避免脏数据被当成幂等命中）；
2. **再幂等**：``OutTradeNo`` 命中已有 ``deliver_event`` → ``E_DELIVER_DUPLICATE``(1402)，
   **不重复发额度**（唯一约束是最后一道：并发下也只可能成功一次）；
3. **发额度/权益**：``quota_pack`` 只加额度；``entitlement`` 延长权益 + 加额度；
4. **绝不写 ``usage_ledger``**：权益发放与语音扣次是两本账（A10）。
   ``usage_ledger`` 的 ``CHECK`` 也只允许 ``CARD_CREATED``，想写歪都过不去。

``query_order`` 查单兜底走**同一个** :meth:`DeliverService.deliver_once`，
不存在第二条发货代码路径 —— 这是「重复发货」类事故的根治办法。
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.pay import DeliverEvent, Entitlement, GoodsItem, VirtualOrder
from app.models.quota import UsageQuota

__all__ = [
    "DELIVER_CHANNEL_CLOUD_FUNCTION",
    "DeliverPayload",
    "DeliverResult",
    "DeliverService",
    "HmacSignatureVerifier",
    "SignatureVerifier",
    "current_period",
]

#: 唯一合法的发货通道（云开发云函数消息推送）
DELIVER_CHANNEL_CLOUD_FUNCTION = "cloudfunc"

#: 明确不支持的通道（云托管消息推送）
_UNSUPPORTED_CHANNELS: frozenset[str] = frozenset({"cloudrun", "container", "cloud_run"})


def current_period(now: datetime | None = None) -> str:
    """自然月归集口径 ``YYYY-MM``。"""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


@dataclass(frozen=True)
class DeliverPayload:
    """一条发货推送（字段名与 ``xpay_*`` 事件保持一致）。"""

    out_trade_no: str
    goods_id: str
    event_type: str = "xpay_goods_deliver_notify"
    tenant_id: str = ""
    member_id: str = ""
    env: str = "prod"
    #: 微信侧签名相关字段（由 verifier 消费，本模块不解释）
    signature: str = ""
    timestamp: str = ""
    nonce: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_push(cls, body: Mapping[str, Any], *, tenant_id: str = "") -> "DeliverPayload":
        """从云函数推送体构造（兼容 ``OutTradeNo`` / ``out_trade_no`` 两种写法）。"""
        raw = dict(body or {})
        out_trade_no = str(raw.get("OutTradeNo") or raw.get("out_trade_no") or "").strip()
        goods_id = str(raw.get("GoodsId") or raw.get("goods_id") or "").strip()
        return cls(
            out_trade_no=out_trade_no,
            goods_id=goods_id,
            event_type=str(raw.get("Event") or raw.get("event_type") or "xpay_goods_deliver_notify"),
            tenant_id=tenant_id or str(raw.get("tenant_id") or ""),
            member_id=str(raw.get("member_id") or ""),
            env=str(raw.get("Env") or raw.get("env") or "prod"),
            signature=str(raw.get("Sign") or raw.get("signature") or raw.get("sign") or ""),
            timestamp=str(raw.get("TimeStamp") or raw.get("timestamp") or ""),
            nonce=str(raw.get("Nonce") or raw.get("nonce") or ""),
            raw=raw,
        )


class SignatureVerifier:
    """验签协议（真实实现需换微信支付平台证书 —— **需核实**）。"""

    def verify(self, payload: DeliverPayload) -> bool:
        """验签：``True`` 通过。"""
        raise NotImplementedError


class HmacSignatureVerifier(SignatureVerifier):
    """本地/HMAC 验签实现（**仅用于沙箱与测试**；生产必须换平台证书验证）。

    规则：``HMAC-SHA256(key, f"{out_trade_no}|{goods_id}|{timestamp}|{nonce}")``
    与 ``signature`` 常量时间比较。

    未配置 ``key`` 时**一律判失败**（fail-closed）：宁可拒发货报警，
    也不能「验签器没配好就当通过」。
    """

    def __init__(self, key: str, *, is_sandbox_only: bool = True) -> None:
        self._key = key or ""
        self.is_sandbox_only = is_sandbox_only

    def sign(self, payload: DeliverPayload) -> str:
        """生成签名（测试与自检用）。"""
        message = "|".join(
            (payload.out_trade_no, payload.goods_id, payload.timestamp, payload.nonce)
        )
        return hmac.new(
            self._key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verify(self, payload: DeliverPayload) -> bool:
        """常量时间比较签名。"""
        if not self._key or not payload.signature:
            return False
        return hmac.compare_digest(self.sign(payload), payload.signature.strip())


@dataclass
class DeliverResult:
    """一次发货处理的结果（**直接可映射错误码**）。"""

    delivered: bool
    reason: str
    error_code: str | None = None
    grant_quota: int = 0
    extend_days: int = 0
    plan_code: str = ""
    out_trade_no: str = ""
    event_id: str | None = None
    #: 显式声明：本流程**永远不写** usage_ledger
    wrote_usage_ledger: bool = False
    detail: str = ""

    @property
    def duplicated(self) -> bool:
        """是否幂等重复（已发过）。"""
        return self.error_code == "E_DELIVER_DUPLICATE"


class DeliverService:
    """发货服务：验签 → 幂等 → 发额度，三步走完即返回。"""

    def __init__(
        self,
        session: Session,
        *,
        verifier: SignatureVerifier,
        allow_envs: tuple[str, ...] = ("prod", "sandbox"),
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
            verifier: 验签器（**必填**，没有验签器不允许发货）。
            allow_envs: 允许的发货环境（沙箱 AppKey 不得发生产单）。
        """
        self._session = session
        self._verifier = verifier
        self._allow_envs = tuple(allow_envs)

    # ------------------------------------------------------------------ 入口
    def handle(
        self,
        payload: DeliverPayload,
        *,
        channel: str = DELIVER_CHANNEL_CLOUD_FUNCTION,
    ) -> DeliverResult:
        """处理一条发货推送（唯一入口）。

        Args:
            payload: 推送内容。
            channel: 消息来源通道；只接受 ``cloudfunc``。

        Returns:
            :class:`DeliverResult`

        Raises:
            ValueError: 通道不受支持（云托管消息推送**不是**发货路径）。
        """
        if (channel or "").strip().lower() in _UNSUPPORTED_CHANNELS:
            raise ValueError(
                "虚拟支付发货只走云开发云函数消息推送（xpay_*）；"
                "云托管消息推送不作为发货路径（会导致重复发货）"
            )
        if not payload.out_trade_no:
            return DeliverResult(
                delivered=False,
                reason="missing_out_trade_no",
                error_code="E_VALIDATION",
                detail="推送缺少 OutTradeNo，无法幂等，直接拒",
            )

        # 1) 验签（失败什么都不写）
        if not self._verifier.verify(payload):
            return DeliverResult(
                delivered=False,
                reason="sign_invalid",
                error_code="E_PAY_SIGN_INVALID",
                out_trade_no=payload.out_trade_no,
                detail="验签失败：不落 deliver_event、不发额度（避免脏数据被当成幂等命中）",
            )

        # 2) 幂等 + 发货（同一实现，查单兜底复用）
        return self.deliver_once(payload)

    def deliver_once(self, payload: DeliverPayload) -> DeliverResult:
        """幂等发货（``query_order`` 兜底**复用同一条路径**）。"""
        tenant_id = _as_uuid(payload.tenant_id)
        existing = self._find_event(tenant_id, payload.out_trade_no)
        if existing is not None:
            return DeliverResult(
                delivered=False,
                reason="duplicate",
                error_code="E_DELIVER_DUPLICATE",
                out_trade_no=payload.out_trade_no,
                event_id=str(existing.event_id),
                grant_quota=int(existing.grant_quota),
                detail="OutTradeNo 已发过货：不重复发额度（幂等）",
            )

        if payload.env not in self._allow_envs:
            return DeliverResult(
                delivered=False,
                reason="env_not_allowed",
                error_code="E_PAY_SIGN_INVALID",
                out_trade_no=payload.out_trade_no,
                detail=f"环境 {payload.env!r} 不允许发货（沙箱密钥不得用于生产）",
            )

        order = self._find_order(tenant_id, payload.out_trade_no)
        if order is not None and str(order.goods_id) != payload.goods_id:
            return DeliverResult(
                delivered=False,
                reason="goods_mismatch",
                error_code="E_PAY_SIGN_INVALID",
                out_trade_no=payload.out_trade_no,
                detail=f"推送商品 {payload.goods_id!r} 与订单商品 {order.goods_id!r} 不一致，拒绝发货",
            )

        goods = self._find_goods(payload.goods_id)
        if goods is None:
            return DeliverResult(
                delivered=False,
                reason="goods_not_found",
                error_code="E_VALIDATION",
                out_trade_no=payload.out_trade_no,
                detail=f"道具 {payload.goods_id!r} 不存在或已下架",
            )

        quota_amount = int(goods.quota_amount or 0)
        validity_days = int(goods.validity_days or 0)
        plan_code = _plan_code_of(goods)
        now = datetime.now(timezone.utc)

        event = DeliverEvent(
            tenant_id=tenant_id,
            out_trade_no=payload.out_trade_no,
            event_type=payload.event_type,
            sign_status="verified",
            deliver_status="delivered",
            grant_quota=quota_amount,
            extend_days=validity_days,
        )
        try:
            with self._session.begin_nested():
                self._session.add(event)
                self._session.flush()
        except IntegrityError:
            # 并发重复推送：仅回滚 SAVEPOINT，不破坏外层事务和 PG app.tenant_id
            return DeliverResult(
                delivered=False,
                reason="duplicate",
                error_code="E_DELIVER_DUPLICATE",
                out_trade_no=payload.out_trade_no,
                detail="并发重复推送被唯一约束拦下，未重复发额度",
            )

        # 3) 发额度 / 权益（**只动 quota 与 entitlement，绝不碰 usage_ledger**）
        if goods.goods_type == "quota_pack":
            self._add_period_quota(tenant_id, quota_amount, now)
        else:
            self._grant_entitlement(tenant_id, plan_code, quota_amount, validity_days, now)
        self._mark_order_paid(tenant_id, payload.out_trade_no, now)

        return DeliverResult(
            delivered=True,
            reason="delivered",
            grant_quota=quota_amount,
            extend_days=validity_days,
            plan_code=plan_code,
            out_trade_no=payload.out_trade_no,
            event_id=str(event.event_id),
            detail=(
                f"已发放 {quota_amount} 次额度"
                + (f"、延长 {validity_days} 天" if validity_days else "")
                + "；本次未写 usage_ledger（发货与扣次是两本账）"
            ),
        )

    # ------------------------------------------------------------------ 内部
    def _find_event(self, tenant_id: uuid.UUID | None, out_trade_no: str) -> DeliverEvent | None:
        """按 ``OutTradeNo`` 找已有发货事件。"""
        statement = (
            select(DeliverEvent)
            .where(DeliverEvent.out_trade_no == out_trade_no)
            .limit(1)
        )
        if tenant_id is not None:
            statement = statement.where(DeliverEvent.tenant_id == tenant_id)
        return self._session.execute(statement).scalar_one_or_none()

    def _find_goods(self, goods_id: str) -> GoodsItem | None:
        """取道具定义（平台表，无租户）。"""
        if not goods_id:
            return None
        statement = select(GoodsItem).where(GoodsItem.goods_id == goods_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def _add_period_quota(self, tenant_id: uuid.UUID | None, amount: int, now: datetime) -> None:
        """加油包：只给当月 ``usage_quota.free_quota`` 加额度。"""
        if tenant_id is None or amount <= 0:
            return
        period = current_period(now)
        quota = self._session.execute(
            select(UsageQuota)
            .where(UsageQuota.tenant_id == tenant_id, UsageQuota.period == period)
            .limit(1)
        ).scalar_one_or_none()
        if quota is None:
            quota = UsageQuota(
                tenant_id=tenant_id,
                period=period,
                free_quota=amount,
                used_count=0,
                degrade_level="L0",
            )
            self._session.add(quota)
        else:
            quota.free_quota = int(quota.free_quota) + amount
        self._session.flush()

    def _grant_entitlement(
        self,
        tenant_id: uuid.UUID | None,
        plan_code: str,
        quota_amount: int,
        validity_days: int,
        now: datetime,
    ) -> None:
        """权益包：延长有效期 + 累加额度（30 天滚动窗口口径）。"""
        if tenant_id is None:
            return
        active = self._session.execute(
            select(Entitlement)
            .where(
                Entitlement.tenant_id == tenant_id,
                Entitlement.plan_code == plan_code,
                Entitlement.status == "active",
            )
            .limit(1)
        ).scalar_one_or_none()
        days = validity_days or 30
        if active is None:
            entitlement = Entitlement(
                tenant_id=tenant_id,
                plan_code=plan_code,
                valid_from=now,
                valid_until=now + timedelta(days=days),
                quota_granted=quota_amount,
                remaining=quota_amount,
                degrade_level="L0",
                status="active",
            )
            self._session.add(entitlement)
        else:
            valid_until = active.valid_until
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            base = valid_until if valid_until > now else now
            active.valid_until = base + timedelta(days=days)
            active.quota_granted = int(active.quota_granted) + quota_amount
            active.remaining = int(active.remaining) + quota_amount
        self._session.flush()

    def _find_order(self, tenant_id: uuid.UUID | None, out_trade_no: str) -> VirtualOrder | None:
        """按订单号查找本地订单用于商品校验。"""
        statement = select(VirtualOrder).where(VirtualOrder.out_trade_no == out_trade_no).limit(1)
        if tenant_id is not None:
            statement = statement.where(VirtualOrder.tenant_id == tenant_id)
        return self._session.execute(statement).scalar_one_or_none()

    def _mark_order_paid(
        self, tenant_id: uuid.UUID | None, out_trade_no: str, now: datetime
    ) -> None:
        """把订单标成已支付（只改支付状态，不参与发货判定）。"""
        statement = select(VirtualOrder).where(VirtualOrder.out_trade_no == out_trade_no).limit(1)
        if tenant_id is not None:
            statement = statement.where(VirtualOrder.tenant_id == tenant_id)
        order = self._session.execute(statement).scalar_one_or_none()
        if order is None:
            return
        order.pay_status = "paid"
        order.paid_at = now
        self._session.flush()


def _plan_code_of(goods: GoodsItem) -> str:
    """从道具推出权益档位（``entitlement`` → 30day，``quota_pack`` → quota_pack）。"""
    goods_id = str(goods.goods_id or "")
    if goods.goods_type == "quota_pack":
        return "quota_pack"
    if "30d" in goods_id or "30day" in goods_id:
        return "30day"
    return "30day"


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """安全转 UUID（推送里可能是字符串，也可能是空）。"""
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None
