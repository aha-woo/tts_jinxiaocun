"""发货回调：**验签失败不发额度**、**OutTradeNo 幂等不重复发**、**只写额度不写流水**（验收项⑥）。

三条红线
--------
1. **验签失败** → ``E_PAY_SIGN_INVALID``(1401)：不落 ``deliver_event``、不发额度。
   连「失败记录」都不写，是为了避免脏数据下次被当成幂等命中（那样会永久拒发真单）；
2. **重复推送** → ``E_DELIVER_DUPLICATE``(1402)：不重复发额度（唯一约束兜底并发）；
3. **只写额度/权益，绝不写 ``usage_ledger``**：权益发放与语音扣次是两本账（A10），
   混在一起会让对账永远对不上。

还有一条容易漏的：**单路径**。
云开发云函数消息推送是唯一发货通道；云托管消息推送**必须**被显式拒绝，
否则两条通道同时开着 = 必然重复发货。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.session import Database
from app.models.pay import DeliverEvent, Entitlement, GoodsItem, VirtualOrder
from app.models.quota import UsageLedger, UsageQuota
from app.models.tenant import Tenant
from app.pay.deliver_callback import (
    DELIVER_CHANNEL_CLOUD_FUNCTION,
    DeliverPayload,
    DeliverService,
    HmacSignatureVerifier,
    current_period,
)

TENANT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
API_KEY = "sandbox-hmac-key-for-t03"
GOODS_ID = "quota_pack_500"


def _payload(
    verifier: HmacSignatureVerifier,
    *,
    out_trade_no: str = "OT202511150001",
    goods_id: str = GOODS_ID,
    sign: bool = True,
) -> DeliverPayload:
    """造一条推送（默认带正确签名）。"""
    raw = DeliverPayload(
        out_trade_no=out_trade_no,
        goods_id=goods_id,
        tenant_id=str(TENANT_ID),
        member_id="member-1",
        env="sandbox",
        timestamp="1731628800",
        nonce="nonce-1",
    )
    if not sign:
        return DeliverPayload(
            out_trade_no=raw.out_trade_no,
            goods_id=raw.goods_id,
            tenant_id=raw.tenant_id,
            member_id=raw.member_id,
            env=raw.env,
            timestamp=raw.timestamp,
            nonce=raw.nonce,
            signature="deadbeef",
        )
    return DeliverPayload(
        out_trade_no=raw.out_trade_no,
        goods_id=raw.goods_id,
        tenant_id=raw.tenant_id,
        member_id=raw.member_id,
        env=raw.env,
        timestamp=raw.timestamp,
        nonce=raw.nonce,
        signature=verifier.sign(raw),
    )


@pytest.fixture()
def database() -> Database:
    """内存库 + 一张 500 次加油包道具。"""
    db = Database.from_url("sqlite+pysqlite:///:memory:")
    db.create_all()
    with db.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_ID, name="测试小店"))
        session.add(
            GoodsItem(
                goods_id=GOODS_ID,
                goods_name="语音加油包（500 次）",
                goods_type="quota_pack",
                price_fen=500,
                quota_amount=500,
                validity_days=0,
                status="on_shelf",
            )
        )
    yield db
    db.dispose()


@pytest.fixture()
def verifier() -> HmacSignatureVerifier:
    """沙箱 HMAC 验签器（生产必须换微信支付平台证书，**需核实**）。"""
    return HmacSignatureVerifier(API_KEY, is_sandbox_only=True)


def _count(database: Database, model) -> int:
    """数行数。"""
    with database.session(bypass_scope=True) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_invalid_signature_is_rejected_and_grants_nothing(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """验签失败 → ``E_PAY_SIGN_INVALID``，不落事件、不发额度、不写流水。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(_payload(verifier, sign=False))

    assert result.delivered is False
    assert result.error_code == "E_PAY_SIGN_INVALID"
    assert result.grant_quota == 0
    assert result.wrote_usage_ledger is False
    assert _count(database, DeliverEvent) == 0, "验签失败不得落 deliver_event"
    assert _count(database, UsageQuota) == 0, "验签失败不得发额度"
    assert _count(database, UsageLedger) == 0


def test_valid_signature_grants_quota_once(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """正常发货：额度到账、事件落库、**流水一行都不写**。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(_payload(verifier))

    assert result.delivered is True
    assert result.grant_quota == 500
    assert result.wrote_usage_ledger is False
    assert _count(database, DeliverEvent) == 1

    with database.session(bypass_scope=True) as session:
        quota = session.execute(select(UsageQuota)).scalar_one()
        assert quota.free_quota == 500
        assert quota.used_count == 0, "发货只加额度，不得动已用次数"
        assert quota.period == current_period()
    assert _count(database, UsageLedger) == 0, "发货绝不写 usage_ledger"


def test_duplicate_push_does_not_grant_again(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """同一 ``OutTradeNo`` 重复推送 → ``E_DELIVER_DUPLICATE``，额度不再涨。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        first = service.handle(_payload(verifier))
        second = service.handle(_payload(verifier))
        third = service.handle(_payload(verifier))

    assert first.delivered is True
    assert second.error_code == "E_DELIVER_DUPLICATE"
    assert third.error_code == "E_DELIVER_DUPLICATE"
    assert second.grant_quota == 500, "重复推送应回显已发额度，但不再累加"

    with database.session(bypass_scope=True) as session:
        quota = session.execute(select(UsageQuota)).scalar_one()
        assert quota.free_quota == 500, f"重复推送把额度加成了 {quota.free_quota}"
    assert _count(database, DeliverEvent) == 1


def test_query_order_fallback_uses_the_same_idempotent_path(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """``query_order`` 查单兜底走**同一条**发货路径：先推过就不会再发一次。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        pushed = service.handle(_payload(verifier))

    # 断线重连后前端调「查单」，服务端内部同样走 deliver_once
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        fallback = service.deliver_once(_payload(verifier))

    assert pushed.delivered is True
    assert fallback.error_code == "E_DELIVER_DUPLICATE"
    with database.session(bypass_scope=True) as session:
        assert session.execute(select(UsageQuota)).scalar_one().free_quota == 500


def test_cloud_run_channel_is_rejected_as_delivery_path(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """**单路径**：云托管消息推送不是发货路径，必须显式拒绝（否则重复发货）。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        with pytest.raises(ValueError) as excinfo:
            service.handle(_payload(verifier), channel="cloudrun")
    assert "云托管" in str(excinfo.value)
    assert _count(database, DeliverEvent) == 0


def test_cloud_function_channel_is_the_default(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """默认通道就是云开发云函数消息推送。"""
    assert DELIVER_CHANNEL_CLOUD_FUNCTION == "cloudfunc"
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(_payload(verifier), channel=DELIVER_CHANNEL_CLOUD_FUNCTION)
    assert result.delivered is True


def test_entitlement_goods_extends_window_and_quota(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """权益类道具：延长有效期 + 累加额度（30 天滚动窗口口径）。"""
    with database.session(bypass_scope=True) as session:
        session.add(
            GoodsItem(
                goods_id="entitlement_30d_basic",
                goods_name="30 天权益 · 基础",
                goods_type="entitlement",
                price_fen=1500,
                quota_amount=800,
                validity_days=30,
                status="on_shelf",
            )
        )

    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(_payload(verifier, goods_id="entitlement_30d_basic", out_trade_no="OT-E-1"))

    assert result.delivered is True
    assert result.extend_days == 30
    with database.session(bypass_scope=True) as session:
        entitlement = session.execute(select(Entitlement)).scalar_one()
        assert entitlement.remaining == 800
        assert entitlement.plan_code == "30day"
    assert _count(database, UsageLedger) == 0


def test_missing_out_trade_no_is_rejected(database: Database, verifier: HmacSignatureVerifier) -> None:
    """没有 ``OutTradeNo`` 就无法幂等，直接拒（不能靠「反正就发一次」赌运气）。"""
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(
            DeliverPayload(out_trade_no="", goods_id=GOODS_ID, tenant_id=str(TENANT_ID))
        )
    assert result.delivered is False
    assert result.error_code == "E_VALIDATION"
    assert _count(database, DeliverEvent) == 0


def test_verifier_without_key_fails_closed(database: Database) -> None:
    """验签器没配密钥 → **一律失败**（fail-closed），绝不默认放行。"""
    verifier = HmacSignatureVerifier("", is_sandbox_only=True)
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        result = service.handle(_payload(HmacSignatureVerifier(API_KEY)))
    assert result.error_code == "E_PAY_SIGN_INVALID"
    assert _count(database, DeliverEvent) == 0


def test_order_is_marked_paid_after_delivery(
    database: Database, verifier: HmacSignatureVerifier
) -> None:
    """发货后订单被标成已支付（只改支付状态，不参与发货判定）。"""
    with database.session(bypass_scope=True) as session:
        session.add(
            VirtualOrder(
                tenant_id=TENANT_ID,
                out_trade_no="OT202511150001",
                goods_id=GOODS_ID,
                price_fen=500,
                env="sandbox",
            )
        )
    with database.session(bypass_scope=True) as session:
        service = DeliverService(session, verifier=verifier)
        assert service.handle(_payload(verifier)).delivered is True

    with database.session(bypass_scope=True) as session:
        order = session.execute(select(VirtualOrder)).scalar_one()
        assert order.pay_status == "paid"
        assert order.paid_at is not None
        assert (datetime.now(timezone.utc) - order.paid_at.replace(tzinfo=timezone.utc)).total_seconds() < 60
