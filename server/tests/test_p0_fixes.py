"""Phase 1 P0 止血回归测试（安全 / 支付 / 限流 / RLS / 数字与口语解析 / 改卡 / 反做 / LLM 适配）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.session import Database
from app.main import create_app
from app.models.inventory import InventoryMovement
from app.models.pay import Entitlement, GoodsItem, VirtualOrder
from app.models.tenant import Tenant
from app.pay.deliver_callback import (
    DeliverPayload,
    DeliverService,
    HmacSignatureVerifier,
)
from app.services.ledger import LedgerService, StoredCardPayload
from app.services.quota import MemoryQuotaStore, QuotaLimits, QuotaService
from app.services.reversal import ReversalService
from app.tenant.rls import tenant_policy_expressions
from app.voice.amount_calculator import compute_amounts
from app.voice.correction_edit import apply_slot_updates
from app.voice.direction import PURCHASE, resolve_direction
from app.voice.llm_provider import RealLlmProvider
from app.voice.number_normalizer import parse_integer_text, parse_money_yuan
from t04_fixtures import create_card_for, seed  # noqa: F401


def test_query_order_rejects_client_paid_true() -> None:
    """客户端传 paid=true 调 /entitlement/query-order 必须被 422 拒绝（封死白嫖漏洞）。"""
    settings = Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-p0-fixes-do-not-reuse",
        wx_provider="fake",
    )
    app = create_app(settings)
    db = app.state.database
    db.create_all()
    try:
        with TestClient(app) as client:
            login_resp = client.post("/api/v1/auth/login", json={"code": "boss-p0"})
            assert login_resp.status_code == 200
            token = login_resp.json()["data"]["jwt"]
            resp = client.post(
                "/api/v1/entitlement/query-order",
                headers={"Authorization": f"Bearer {token}"},
                json={"out_trade_no": "OT-FAKE-001", "paid": True},
            )
            assert resp.status_code == 422
    finally:
        db.drop_all()
        db.dispose()


def test_deliver_rejects_goods_mismatch_and_handles_naive_valid_until() -> None:
    """回调商品与订单不匹配拒绝发货；续费时 naive valid_until 不再抛 TypeError。"""
    db = Database.from_url("sqlite+pysqlite:///:memory:")
    db.create_all()
    tenant_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    verifier = HmacSignatureVerifier("test-key", is_sandbox_only=True)
    try:
        with db.session(bypass_scope=True) as session:
            session.add(Tenant(tenant_id=tenant_id, name="P0测试店"))
            session.add(
                GoodsItem(
                    goods_id="entitlement_30d_basic",
                    goods_name="基础 30 天",
                    goods_type="entitlement",
                    price_fen=1900,
                    quota_amount=1500,
                    validity_days=30,
                    status="on_shelf",
                )
            )
            session.add(
                GoodsItem(
                    goods_id="entitlement_30d_pro",
                    goods_name="专业 30 天",
                    goods_type="entitlement",
                    price_fen=4900,
                    quota_amount=5000,
                    validity_days=30,
                    status="on_shelf",
                )
            )
            session.add(
                VirtualOrder(
                    tenant_id=tenant_id,
                    out_trade_no="OT-MISMATCH",
                    goods_id="entitlement_30d_basic",
                    price_fen=1900,
                    env="sandbox",
                )
            )
            # 模拟历史 naive datetime 权益记录
            session.add(
                Entitlement(
                    tenant_id=tenant_id,
                    plan_code="30day",
                    valid_from=datetime(2026, 1, 1, 0, 0, 0),
                    valid_until=datetime(2026, 12, 31, 0, 0, 0),
                    quota_granted=100,
                    remaining=100,
                    status="active",
                )
            )

        with db.session(bypass_scope=True) as session:
            svc = DeliverService(session, verifier=verifier)
            mismatch = svc.deliver_once(
                DeliverPayload(
                    out_trade_no="OT-MISMATCH",
                    goods_id="entitlement_30d_pro",
                    tenant_id=str(tenant_id),
                    env="sandbox",
                )
            )
            assert mismatch.delivered is False
            assert mismatch.reason == "goods_mismatch"

            # 续费不抛 TypeError
            renew = svc.deliver_once(
                DeliverPayload(
                    out_trade_no="OT-RENEW-OK",
                    goods_id="entitlement_30d_basic",
                    tenant_id=str(tenant_id),
                    env="sandbox",
                )
            )
            assert renew.delivered is True
    finally:
        db.dispose()


def test_rls_nullable_tenant_with_check_forbids_null_writes() -> None:
    """可空租户表的 WITH CHECK 不得允许写入 tenant_id IS NULL。"""
    using_expr, check_expr = tenant_policy_expressions("template_rule", nullable_tenant=True)
    assert "tenant_id IS NULL" in using_expr
    assert "IS NULL" not in check_expr


def test_quota_day_bucket_persists_across_minutes_and_minute_reject_does_not_burn_day() -> None:
    """日桶跨分钟不重置，且分钟限流拒绝不消耗日桶额度。"""
    svc = QuotaService(MemoryQuotaStore(), limits=QuotaLimits(per_minute=2, per_day=5))
    t0 = 1_700_000_000.0
    assert svc.take("t1", now=t0).allowed is True
    assert svc.take("t1", now=t0 + 1).allowed is True
    # 第 3 次被分钟桶拒，日桶应仍剩 3（5 - 2）
    rejected = svc.take("t1", now=t0 + 2)
    assert rejected.allowed is False
    assert rejected.scope == "min"
    assert svc.peek("t1", now=t0 + 2)["day"] == 3

    # 下一分钟继续拿 3 次，第 6 次（累计第 6 次）必须被日桶拒
    assert svc.take("t1", now=t0 + 65).allowed is True
    assert svc.take("t1", now=t0 + 66).allowed is True
    assert svc.take("t1", now=t0 + 130).allowed is True
    day_rejected = svc.take("t1", now=t0 + 131)
    assert day_rejected.allowed is False
    assert day_rejected.scope == "day"


def test_number_normalizer_zero_and_fen_cases() -> None:
    """修复「一百零五→150」及「一块二毛五→1.2」错误。"""
    assert parse_integer_text("一百零五") == 105
    assert parse_integer_text("两百零八") == 208
    assert parse_integer_text("一千零五") == 1005
    assert parse_integer_text("一万零五") == 10005
    assert parse_integer_text("一百二") == 120
    assert parse_money_yuan("一块二毛五").value == Decimal("1.25")


def test_cash_purchase_and_single_unit_total_have_no_false_conflicts() -> None:
    """「进了十箱可乐，现结」与「进了一箱可乐，一共35块」不再误报冲突。"""
    v1 = resolve_direction("进了十箱可乐，现结")
    assert v1.direction == PURCHASE and v1.conflict is False

    v2 = resolve_direction("进了两箱啤酒，付了70")
    assert v2.direction == PURCHASE and v2.conflict is False

    amt = compute_amounts("进了一箱可乐，一共35块", quantity=Decimal("1"), unit_label="箱")
    assert amt.needs_clarify is False
    assert amt.ta_fen == 3500
    assert amt.up_fen == 3500


def test_correction_edit_recalculates_ta_preserves_expiry_and_handles_decimals() -> None:
    """改 Q/UP 必须重算 TA，支持小数数量，并保留 expires_at。"""
    payload = StoredCardPayload(
        intent_id="purchase.create",
        slots={"P": "鲈鱼", "Q": 2, "U": "斤", "UP": 1000, "TA": 2000},
        source={"P": "asr", "Q": "asr", "U": "asr", "UP": "asr", "TA": "computed"},
        lines=(
            {
                "product_id": None,
                "product_label": "鲈鱼",
                "quantity": 2,
                "unit": "斤",
                "qty_base": "2",
                "unit_price_fen": 1000,
                "amount_fen": 2000,
            },
        ),
        expires_at="2026-12-31T00:00:00+00:00",
    )
    edited = apply_slot_updates(payload=payload, updates={"Q": Decimal("2.5")})
    assert edited.payload.expires_at == "2026-12-31T00:00:00+00:00"
    assert edited.payload.lines[0]["amount_fen"] == 2500
    assert edited.payload.slots["TA"] == 2500


def test_sale_reversal_writes_positive_movement(seed) -> None:  # noqa: F811
    """撤销销售单必须写正数库存流水，确保销售与冲正流水总和净值为 0。"""
    t0 = datetime.now(timezone.utc)
    # 先入库 2 箱（48 瓶），保证销售单有库存可出
    with seed.database.session(context=seed.context) as session:
        _, purchase_creation = create_card_for(
            seed,
            session=session,
            intent_id="purchase.create",
            slots={"P": "可乐", "Q": 2, "U": "箱", "UP": 3500, "TA": 7000, "PS": "已结"},
            now=t0,
        )
        p_out = LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(purchase_creation.card_id),
            confirm_type="button",
            now=t0 + timedelta(seconds=1),
        )
        assert p_out.confirmed is True

    with seed.database.session(context=seed.context) as session:
        _, creation = create_card_for(
            seed,
            session=session,
            intent_id="sales.create",
            slots={"P": "可乐", "Q": 2, "U": "瓶", "UP": 350, "TA": 700, "PS": "已结"},
            now=t0 + timedelta(seconds=10),
        )
        outcome = LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(creation.card_id),
            confirm_type="button",
            now=t0 + timedelta(seconds=11),
        )
        assert outcome.confirmed is True
        doc_id = uuid.UUID(str(outcome.doc_id))

    with seed.database.session(context=seed.context) as session:
        rev_out = ReversalService(session).reverse(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            doc_id=doc_id,
            member_id=seed.member_id,
            reason="退回",
            now=t0 + timedelta(minutes=1),
        )
        assert rev_out.reversal_doc_id is not None
        rev_doc_id = uuid.UUID(str(rev_out.reversal_doc_id))

    with seed.database.session(bypass_scope=True) as session:
        moves = list(session.execute(select(InventoryMovement)).scalars())
        sale_and_rev = [m for m in moves if m.doc_id in {doc_id, rev_doc_id}]
        assert len(sale_and_rev) == 2
        assert any(Decimal(m.qty_base) > 0 for m in sale_and_rev if m.doc_id == rev_doc_id)
        net = sum(Decimal(m.qty_base) for m in sale_and_rev)
        assert net == Decimal("0"), f"销售撤单流水未归零，净值={net}"
        # 总库存恢复为入库的 48 瓶
        assert sum(Decimal(m.qty_base) for m in moves) == Decimal("48")


def test_real_llm_provider_compatible_response_format_and_strips_think() -> None:
    """RealLlmProvider 发送合法 OpenAI response_format 并剥离 <think> 与 Markdown 围栏。"""
    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '<think>分析中</think>\n```json\n{"intent": "purchase.create"}\n```'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        return _FakeResp()

    provider = RealLlmProvider(api_key="k", base_url="https://example.com/v1", model="qwen2.5-14b")
    with patch("httpx.post", side_effect=_fake_post):
        out = provider.parse("进了两箱可乐", {"type": "object"})

    assert out == {"intent": "purchase.create"}
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"] == {"type": "json_object"}
