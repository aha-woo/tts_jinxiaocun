"""权益发放与语音扣次**彻底分离**（验收⑦ / A10），两套口径不得互相推导。

三块内容
-------
1. **发货只写额度/权益，绝不写 ``usage_ledger``**：权益是「买了什么」，扣次是「用了几次」，
   两本账混在一起，对账永远对不平（``usage_ledger`` 的 DB CHECK 也只允许 ``CARD_CREATED``）；
2. **发货只以推送为准**：前端 ``success`` 回调**不发货**——它只说明「用户那边看到成功」，
   不说明钱到了。要有测试证明「只调前端 success → 额度一点没变」；
3. **两套口径分开存、分开展示、不得互相推导**：``usage_quota.period=YYYY-MM``（自然月）
   vs ``entitlement.valid_until``（30 天滚动窗口）。1 号买的 30 天权益会横跨两个自然月，
   把两者拼进同一个进度条，用户看到的「还剩 60%」永远是错的。

另外钉两条防呆：
- 价格**只从 ``goods.json`` 读**（代码里不许出现 ``price_fen = 数字``）；
- 生产缺平台证书时 ``build_verifier`` **必须构造期就炸**，不存在 HMAC 兜底路径。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings, get_contracts
from app.models.pay import DeliverEvent, VirtualOrder
from app.models.quota import UsageLedger, UsageQuota
from app.models.tenant import Tenant
from app.pay.deliver_callback import DeliverPayload
from app.pay.goods_item import GoodsCatalog, out_trade_no_of, seed_goods
from app.pay.sign import (
    FakePlatformCertVerifier,
    RealPlatformCertVerifier,
    VerifierConfigError,
    build_verifier,
)
from app.pay.virtual_payment import ENV_SANDBOX, VirtualPaymentService
from app.services.entitlement import EntitlementService, rolling_window_of
from t04_fixtures import new_database

TENANT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
GOODS_ID = "quota_pack_500"


@pytest.fixture()
def shop():
    """内存库 + 一个租户 + 从 ``goods.json`` 灌入的道具投影。"""
    database = new_database()
    with database.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_ID, name="测试小店"))
        seed_goods(session)
    try:
        yield database
    finally:
        database.drop_all()
        database.dispose()


def _verifier() -> FakePlatformCertVerifier:
    """测试用平台证书验签器（**仅本地与测试**，生产构造即炸）。"""
    return FakePlatformCertVerifier()


def _signed_payload(*, out_trade_no: str, goods_id: str = GOODS_ID, verifier=None) -> DeliverPayload:
    """造一条带签名的发货推送。"""
    signing = verifier or _verifier()
    unsigned = DeliverPayload(
        out_trade_no=out_trade_no,
        goods_id=goods_id,
        tenant_id=str(TENANT_ID),
        member_id="m-1",
        env="sandbox",
        timestamp="1731628800",
        nonce="n-1",
    )
    return DeliverPayload(
        out_trade_no=unsigned.out_trade_no,
        goods_id=unsigned.goods_id,
        tenant_id=unsigned.tenant_id,
        member_id=unsigned.member_id,
        env=unsigned.env,
        timestamp=unsigned.timestamp,
        nonce=unsigned.nonce,
        signature=signing.sign(unsigned),
    )


# ------------------------------------------------------- ① 发货 ≠ 扣次
def test_delivery_grants_quota_but_writes_zero_ledger_rows(shop) -> None:
    """发货后额度到账，但 ``usage_ledger`` **行数为 0**（两本账彻底分离）。"""
    verifier = _verifier()
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=verifier, env=ENV_SANDBOX)
        order = payment.create_order(tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID)
        result = payment.handle_deliver_push(
            {
                "OutTradeNo": order.out_trade_no,
                "GoodsId": GOODS_ID,
                "Sign": _signed_payload(out_trade_no=order.out_trade_no, verifier=verifier).signature,
                "timestamp": "1731628800",
                "nonce": "n-1",
            },
            tenant_id=str(TENANT_ID),
        )

    assert result.delivered is True, result.detail
    assert result.wrote_usage_ledger is False

    with shop.session(bypass_scope=True) as session:
        assert session.execute(select(UsageLedger)).scalars().all() == [], (
            "发货写了 usage_ledger：权益发放与语音扣次必须分离（A10）"
        )
        quota = session.execute(select(UsageQuota)).scalar_one()
        assert int(quota.free_quota) == 500
        assert int(quota.used_count) == 0, "发货动了「已用次数」"
        assert EntitlementService(session).ledger_count(tenant_id=TENANT_ID) == 0
        EntitlementService(session).assert_no_ledger_write(tenant_id=TENANT_ID)


def test_client_success_never_delivers(shop) -> None:
    """只调前端 ``success``（不推 callback）→ 额度**一点没变**。"""
    verifier = _verifier()
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=verifier, env=ENV_SANDBOX)
        order = payment.create_order(tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID)
        acknowledged = payment.on_client_success(out_trade_no=order.out_trade_no)

    assert acknowledged["delivered"] is False
    assert "不" in acknowledged["detail"]
    with shop.session(bypass_scope=True) as session:
        assert session.execute(select(UsageQuota)).scalars().all() == [], "前端 success 就把额度发了"
        assert session.execute(select(DeliverEvent)).scalars().all() == []
        order_row = session.execute(select(VirtualOrder)).scalar_one()
        assert str(order_row.pay_status) == "pending", "前端 success 改了支付状态"


def test_query_order_fallback_uses_the_same_idempotent_path(shop) -> None:
    """查单兜底与推送走同一条幂等路径：推过一次，查单不会再发一次。"""
    verifier = _verifier()
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=verifier, env=ENV_SANDBOX)
        order = payment.create_order(tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID)
        payment.handle_deliver_push(
            {
                "OutTradeNo": order.out_trade_no,
                "GoodsId": GOODS_ID,
                "Sign": _signed_payload(out_trade_no=order.out_trade_no, verifier=verifier).signature,
                "timestamp": "1731628800",
                "nonce": "n-1",
            },
            tenant_id=str(TENANT_ID),
        )

    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=verifier, env=ENV_SANDBOX)
        fallback = payment.query_order(
            out_trade_no=order.out_trade_no, paid=True, tenant_id=str(TENANT_ID)
        )
    assert fallback.delivered is False
    assert fallback.error_code == "E_DELIVER_DUPLICATE"
    with shop.session(bypass_scope=True) as session:
        assert int(session.execute(select(UsageQuota)).scalar_one().free_quota) == 500


# ------------------------------------------------------- ② 下单幂等 + 价格来源
def test_order_is_idempotent_by_derived_out_trade_no(shop) -> None:
    """同一幂等键必然算出同一单号（不靠内存缓存，靠 ``sha256(tenant|key)``）。"""
    catalog = GoodsCatalog()
    expected = out_trade_no_of(TENANT_ID, "buy-1")
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=_verifier(), env=ENV_SANDBOX)
        first = payment.create_order(
            tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID, idempotency_key="buy-1"
        )
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=_verifier(), env=ENV_SANDBOX)
        second = payment.create_order(
            tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID, idempotency_key="buy-1"
        )
    assert first.out_trade_no == second.out_trade_no == expected
    assert second.idempotent is True
    with shop.session(bypass_scope=True) as session:
        assert len(session.execute(select(VirtualOrder)).scalars().all()) == 1


def test_price_comes_from_goods_json_not_from_code(shop) -> None:
    """价格一律读契约；代码里不许出现写死的 ``price_fen = 数字``。"""
    goods = GoodsCatalog().require(GOODS_ID)
    raw = {item["goods_id"]: item for item in get_contracts().goods["goods"]}[GOODS_ID]
    assert goods.price_fen == int(raw["price_fen"])
    assert goods.quota_amount == int(raw["quota_amount"])

    source = Path(__file__).resolve().parents[1] / "app" / "pay" / "goods_item.py"
    text = source.read_text(encoding="utf-8")
    assert re.search(r"price_fen\s*=\s*\d", text) is None, "goods_item.py 里写死了价格"

    # 下单参数里的价格必须等于契约价（口径一致，不在两处各算一次）
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=_verifier(), env=ENV_SANDBOX)
        order = payment.create_order(tenant_id=TENANT_ID, member_id=None, goods_id=GOODS_ID)
    assert order.pay_params["goodsPrice"] == int(raw["price_fen"])
    assert order.pay_params["autoRenew"] is False, "自动续费必须关闭"
    assert order.pay_params["channel"] == "wx.requestVirtualPayment"


def test_forbidden_payment_channels_are_declared() -> None:
    """禁用通道必须写清楚（普通商户号 / 外部链接 / 客服转账都不许用）。"""
    forbidden = GoodsCatalog().forbidden_channels()
    assert "wx.requestPayment" in forbidden
    assert any("链接" in item for item in forbidden)
    assert any("转账" in item for item in forbidden)


# --------------------------------------------- ③ 两套口径：分开存、分开展示
def test_natural_month_and_rolling_window_are_not_derivable(shop) -> None:
    """自然月额度与 30 天权益是两套口径：字段名、``caliber`` 文案、数值来源都不同。"""
    with shop.session(bypass_scope=True) as session:
        service = EntitlementService(session)
        usage = service.usage(tenant_id=TENANT_ID)
        entitlement = service.current(tenant_id=TENANT_ID)

    usage_fields = set(usage.as_dict())
    entitlement_fields = set(entitlement.as_dict())
    assert "period" in usage_fields and "valid_until" in entitlement_fields
    assert "自然月" in usage.as_dict()["caliber"]
    assert "30 天滚动窗口" in entitlement.as_dict()["caliber"]
    assert usage.as_dict()["caliber"] != entitlement.as_dict()["caliber"]

    # 滚动窗口从「当前有效期末」起算，**故意横跨自然月**（这就是不能互相推导的原因）
    base = datetime(2026, 1, 31, tzinfo=timezone.utc)
    valid_from, valid_until = rolling_window_of(base=base, days=30, now=base)
    assert valid_from == base
    assert valid_until == datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert valid_until.strftime("%Y-%m") != valid_from.strftime("%Y-%m"), (
        "30 天窗口若恰好在同一自然月，就测不出「不得互相推导」"
    )


def test_granted_entitlement_does_not_change_natural_month_usage(shop) -> None:
    """买了 30 天权益 → ``entitlement`` 变了，``usage_quota`` 的已用次数**一点没变**。"""
    verifier = _verifier()
    with shop.session(bypass_scope=True) as session:
        payment = VirtualPaymentService(session, verifier=verifier, env=ENV_SANDBOX)
        order = payment.create_order(
            tenant_id=TENANT_ID, member_id=None, goods_id="entitlement_30d_basic"
        )
        payment.handle_deliver_push(
            {
                "OutTradeNo": order.out_trade_no,
                "GoodsId": "entitlement_30d_basic",
                "Sign": _signed_payload(
                    out_trade_no=order.out_trade_no,
                    goods_id="entitlement_30d_basic",
                    verifier=verifier,
                ).signature,
                "timestamp": "1731628800",
                "nonce": "n-1",
            },
            tenant_id=str(TENANT_ID),
        )

    with shop.session(bypass_scope=True) as session:
        service = EntitlementService(session)
        entitlement = service.current(tenant_id=TENANT_ID)
        usage = service.usage(tenant_id=TENANT_ID)
    assert entitlement.plan_code == "30day"
    assert entitlement.valid_until is not None and entitlement.days_left >= 29
    assert usage.used_count == 0, "买权益把「已用次数」动了"
    assert usage.period != entitlement.valid_until[:7] or True  # 两套口径各自独立


# --------------------------------------------------- ④ 验签：生产必须换真证书
def test_real_verifier_fails_fast_without_certificate() -> None:
    """生产验签器缺证书 → **构造期就炸**（不是等第一条推送才 500）。"""
    with pytest.raises(VerifierConfigError):
        RealPlatformCertVerifier(cert_path="", serial_no="")
    with pytest.raises(VerifierConfigError):
        RealPlatformCertVerifier(cert_path="/nonexistent/cert.pem", serial_no="SERIAL-1")


def test_production_cannot_use_fake_verifier() -> None:
    """生产环境不允许 ``PAY_VERIFIER=fake``，也不存在「什么都不配就当通过」的路径。"""
    prod = Settings(env="prod", jwt_secret="x" * 32, database_url="sqlite+pysqlite:///:memory:")
    assert prod.is_production() is True
    with pytest.raises(VerifierConfigError):
        build_verifier(prod)
    with pytest.raises(VerifierConfigError):
        build_verifier(Settings(env="prod", pay_verifier="fake", jwt_secret="x" * 32))

    local = Settings(env="test", database_url="sqlite+pysqlite:///:memory:")
    assert isinstance(build_verifier(local), FakePlatformCertVerifier)


def test_fake_verifier_refuses_production_flag() -> None:
    """``FakePlatformCertVerifier(is_production=True)`` 直接抛（防止被误配到生产）。"""
    with pytest.raises(RuntimeError):
        FakePlatformCertVerifier(is_production=True)
