"""虚拟支付与权益：entitlement / goods_item / virtual_order / deliver_event。

铁律（A10 / D12 / D13）
--------------------
- `goods_item` 是**平台级**（无 tenant_id），道具定义单一真相源是
  `packages/shared/contracts/goods.json`，此表只是它的只读投影（种子从 JSON 灌入）；
- `virtual_order` / `deliver_event` 的 `UNIQUE(tenant_id, out_trade_no)` 是
  「重复推送不重复发额度」的落地点；
- 发货**只写额度/权益**，绝不写 `usage_ledger`；前端 `success` 不作发货依据；
- `auto_renew` 在本项目**恒为 false**（取消自动续费），故不建该列，避免被误用。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, created_at_column, uuid_pk


class Entitlement(Base, TenantMixin):
    """权益（30 天滚动窗口）：到期不自动扣费，靠自有推送提醒复购。"""

    __tablename__ = "entitlement"

    entitlement_id: Mapped[uuid.UUID] = uuid_pk()
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False, server_default="free")
    valid_from: Mapped[datetime] = mapped_column(nullable=False)
    valid_until: Mapped[datetime] = mapped_column(nullable=False)
    #: 付费已发放语音额度（与 usage_ledger 的消耗口径分离）
    quota_granted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    remaining: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    degrade_level: Mapped[str] = mapped_column(String(2), nullable=False, server_default="L0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_code", "valid_from", name="uq_entitlement_tenant_id_plan_code_valid_from"),
        Index("ix_entitlement_tenant_id_valid_until_status", "tenant_id", "valid_until", "status"),
        CheckConstraint("plan_code IN ('free','30day','quota_pack')", name="ck_entitlement_plan_code"),
        CheckConstraint("status IN ('active','expired')", name="ck_entitlement_status"),
        CheckConstraint("quota_granted >= 0 AND remaining >= 0", name="ck_entitlement_quota"),
        CheckConstraint("valid_until >= valid_from", name="ck_entitlement_window"),
    )


class GoodsItem(Base):
    """道具定义（**平台级，无 tenant_id**）。种子来自 goods.json，服务端不复制价格常量。"""

    __tablename__ = "goods_item"

    goods_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goods_name: Mapped[str] = mapped_column(Text, nullable=False)
    goods_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 最低 1 元 = 100 分【需核实】
    price_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    quota_amount: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="on_shelf")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_goods_item_goods_type_status", "goods_type", "status"),
        CheckConstraint("goods_type IN ('entitlement','quota_pack')", name="ck_goods_item_type"),
        CheckConstraint("price_fen >= 100", name="ck_goods_item_min_price"),
        CheckConstraint("status IN ('on_shelf','off_shelf')", name="ck_goods_item_status"),
    )


class VirtualOrder(Base, TenantMixin):
    """虚拟支付订单：`out_trade_no` 为发货幂等键；`env` 区分现网/沙箱。"""

    __tablename__ = "virtual_order"

    order_id: Mapped[uuid.UUID] = uuid_pk()
    out_trade_no: Mapped[str] = mapped_column(String(64), nullable=False)
    goods_id: Mapped[str] = mapped_column(String(64), nullable=False)
    price_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pay_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    #: prod / sandbox：沙箱 AppKey 不得用于生产
    env: Mapped[str] = mapped_column(String(16), nullable=False, server_default="prod")
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "out_trade_no", name="uq_virtual_order_tenant_id_out_trade_no"),
        Index("ix_virtual_order_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint("pay_status IN ('pending','paid','closed')", name="ck_virtual_order_pay_status"),
        CheckConstraint("env IN ('prod','sandbox')", name="ck_virtual_order_env"),
        CheckConstraint("price_fen >= 0", name="ck_virtual_order_price_nonneg"),
    )


class DeliverEvent(Base, TenantMixin):
    """发货事件（幂等去重）。重复推送 INSERT 冲突 → ``E_DELIVER_DUPLICATE``，不重复发额度。"""

    __tablename__ = "deliver_event"

    event_id: Mapped[uuid.UUID] = uuid_pk()
    out_trade_no: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: verified / invalid（验签失败 → E_PAY_SIGN_INVALID，不落发放）
    sign_status: Mapped[str] = mapped_column(String(16), nullable=False)
    deliver_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="delivered")
    grant_quota: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    extend_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("tenant_id", "out_trade_no", name="uq_deliver_event_tenant_id_out_trade_no"),
        Index("ix_deliver_event_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint("sign_status IN ('verified','invalid')", name="ck_deliver_event_sign_status"),
        CheckConstraint("deliver_status IN ('delivered','duplicate','failed')", name="ck_deliver_event_status"),
        CheckConstraint("grant_quota >= 0 AND extend_days >= 0", name="ck_deliver_event_grant"),
    )
