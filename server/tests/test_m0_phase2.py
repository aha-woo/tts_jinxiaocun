"""Phase 2 (M0) 核心账本重构与 RBAC/UI 联调回归测试。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import Settings
from app.domain.doc_service import DocDraft, DocLineDraft, DocService
from app.domain.report_service import ReportService
from app.main import create_app
from app.models.catalog import Product
from app.models.doc import ArAp, DocHeader, Payment
from app.models.inventory import CostAdjustment, Inventory, InventoryMovement, StockBatch
from app.models.ops import AuditLog, DeletionRequest, Gap
from app.models.org import Member, Role, Store
from app.models.tenant import Tenant
from app.services.auth import AuthService
from app.services.ledger import LedgerService
from app.services.wx_provider import FakeWxProvider
from app.utils.date_utils import today_in_tz
from t04_fixtures import create_card_for, seed  # noqa: F401


def _make_app():
    app = create_app(
        Settings(
            env="test",
            database_url="sqlite+pysqlite:///:memory:",
            jwt_secret="test-secret-for-m0-phase2-not-for-prod",
            wx_provider="fake",
        )
    )
    app.state.database.create_all()
    return app


def _login_headers(client: TestClient, code: str) -> tuple[dict[str, str], dict]:
    resp = client.post("/api/v1/auth/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return {"Authorization": f"Bearer {data['jwt']}"}, data


def _add_member_and_token(
    app, tenant_id: uuid.UUID, store_id: uuid.UUID, code: str, role_code: str
) -> tuple[uuid.UUID, dict[str, str]]:
    db = app.state.database
    member_id = uuid.uuid4()
    provider = FakeWxProvider()
    identity = provider.resolve_openid(code)
    with db.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, tenant_id)
        store = session.get(Store, store_id)
        assert tenant is not None and store is not None
        member = Member(
            member_id=member_id,
            tenant_id=tenant_id,
            wx_openid=identity.openid,
            name=f"员工-{role_code}",
            status="active",
        )
        role = Role(
            role_id=uuid.uuid4(),
            tenant_id=tenant_id,
            member_id=member_id,
            store_id=None,
            role_code=role_code,
        )
        session.add(member)
        session.add(role)
        session.flush()
        svc = AuthService(db, app.state.settings, provider)
        issued = svc._issue(tenant, store, member, role, "free")
        return member_id, {"Authorization": f"Bearer {issued.token}"}


def test_negative_stock_virtual_batch_and_auto_settlement() -> None:
    """默认允许负库存：超卖写「未到货」虚拟批与暂估流水；补进货自动抵扣写 CostAdjustment 并通过核对。"""
    app = _make_app()
    db = app.state.database
    try:
        with TestClient(app) as client:
            headers, info = _login_headers(client, "boss-neg-1")
            tenant_id = uuid.UUID(info["tenant_id"])
            store_id = uuid.UUID(info["store_id"])
            member_id = uuid.UUID(info["member_id"])

            # 建一个开启批次管理、有最近进价 800 分的商品
            prod_id = uuid.uuid4()
            with db.session(bypass_scope=True) as session:
                session.add(
                    Product(
                        tenant_id=tenant_id,
                        product_id=prod_id,
                        name="鲈鱼",
                        base_unit="斤",
                        last_purchase_price_fen=800,
                        last_sale_price_fen=1500,
                        track_batch=True,
                        cost_method="batch",
                        status="active",
                    )
                )

            # 0 库存先卖 5 斤
            s1 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=headers)
            sid = s1.json()["data"]["session_id"]
            p1 = client.post(
                f"/api/v1/voice/sessions/{sid}/parse",
                json={"asr_text": "卖了五斤鲈鱼，一斤十五元"},
                headers=headers,
            )
            cid = p1.json()["data"]["card"]["meta"]["card_id"]
            c1 = client.post(
                f"/api/v1/voice/cards/{cid}/confirm",
                json={"confirm_type": "button", "button_fallback": True},
                headers=headers,
            )
            assert c1.status_code == 200, c1.text
            sale_doc_id = uuid.UUID(c1.json()["data"]["result"]["doc_id"])

            with db.session(bypass_scope=True) as session:
                inv = session.execute(
                    select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == prod_id)
                ).scalar_one()
                assert Decimal(inv.qty_base) == Decimal("-5")
                assert inv.negative_flag is True
                vb = session.execute(
                    select(StockBatch).where(
                        StockBatch.tenant_id == tenant_id,
                        StockBatch.product_id == prod_id,
                        StockBatch.is_virtual.is_(True),
                    )
                ).scalar_one()
                assert Decimal(vb.qty_left) == Decimal("-5")
                assert vb.label == "未到货"
                sale_move = session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.doc_id == sale_doc_id,
                    )
                ).scalar_one()
                assert sale_move.cost_status == "provisional"
                assert sale_move.unit_cost_fen == 800
                gap = session.execute(
                    select(Gap).where(
                        Gap.tenant_id == tenant_id,
                        Gap.kind == "negative_stock",
                        Gap.ref_id == prod_id,
                        Gap.status == "open",
                    )
                ).scalar_one()
                assert "负库存" in gap.title

            # 补进货 10 斤 × 10 元（1000 分）
            s2 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=headers)
            sid2 = s2.json()["data"]["session_id"]
            p2 = client.post(
                f"/api/v1/voice/sessions/{sid2}/parse",
                json={"asr_text": "进了十斤鲈鱼，一斤十元"},
                headers=headers,
            )
            cid2 = p2.json()["data"]["card"]["meta"]["card_id"]
            c2 = client.post(
                f"/api/v1/voice/cards/{cid2}/confirm",
                json={"confirm_type": "button", "button_fallback": True},
                headers=headers,
            )
            assert c2.status_code == 200, c2.text

            with db.session(bypass_scope=True) as session:
                inv = session.execute(
                    select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == prod_id)
                ).scalar_one()
                assert Decimal(inv.qty_base) == Decimal("5")
                assert inv.negative_flag is False

                vb = session.execute(
                    select(StockBatch).where(
                        StockBatch.tenant_id == tenant_id,
                        StockBatch.product_id == prod_id,
                        StockBatch.is_virtual.is_(True),
                    )
                ).scalar_one()
                assert Decimal(vb.qty_left) == Decimal("0")
                assert vb.status == "exhausted"

                real_batch = session.execute(
                    select(StockBatch).where(
                        StockBatch.tenant_id == tenant_id,
                        StockBatch.product_id == prod_id,
                        StockBatch.is_virtual.is_(False),
                    )
                ).scalar_one()
                assert Decimal(real_batch.qty_in) == Decimal("10")
                assert Decimal(real_batch.qty_left) == Decimal("5")

                adj = session.execute(
                    select(CostAdjustment).where(CostAdjustment.tenant_id == tenant_id)
                ).scalar_one()
                assert adj.reason == "oversold_settle"
                assert adj.old_unit_cost_fen == 800
                assert adj.new_unit_cost_fen == 1000
                assert adj.delta_cogs_fen == 1000  # (1000 - 800) * 5

                sale_move = session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.doc_id == sale_doc_id,
                    )
                ).scalar_one()
                assert sale_move.cost_status == "final"
                assert sale_move.unit_cost_fen == 1000

                sale_doc = session.get(DocHeader, sale_doc_id)
                assert sale_doc is not None and int(sale_doc.cogs_fen or 0) == 5000

                open_neg_gaps = list(
                    session.execute(
                        select(Gap).where(
                            Gap.tenant_id == tenant_id,
                            Gap.kind == "negative_stock",
                            Gap.status == "open",
                        )
                    ).scalars()
                )
                assert open_neg_gaps == []

            rec = client.get("/api/v1/inventory/reconcile", headers=headers)
            assert rec.status_code == 200, rec.text
            assert rec.json()["data"]["ok"] is True
            assert rec.json()["data"]["unsettled_cost_movements"] == 0
    finally:
        db.drop_all()
        db.dispose()


def test_moving_avg_ignores_null_cost_and_resolves_gap_on_patch() -> None:
    """缺进价进货生成 draft 商品与 missing_cost 待处理，不按 0 稀释均价；补双价自动转正并清待处理。"""
    app = _make_app()
    db = app.state.database
    try:
        with TestClient(app) as client:
            headers, info = _login_headers(client, "boss-null-1")
            tenant_id = uuid.UUID(info["tenant_id"])
            store_id = uuid.UUID(info["store_id"])
            member_id = uuid.UUID(info["member_id"])

            # 1) 不带进价入库 10 瓶矿泉水
            from app.tenant.context import TenantContext
            from t04_fixtures import parse_of, preview_for

            class _SeedLike:
                pass

            ctx = TenantContext(
                tenant_id=tenant_id,
                store_id=store_id,
                member_id=member_id,
                role_code="boss",
                tenant_version=1,
            )
            with db.session(context=ctx) as session:
                ledger = LedgerService(session)
                from app.voice.impact_preview import PreviewBundle

                parse = parse_of("purchase.create", slots={"P": "矿泉水", "Q": 10, "U": "瓶"})
                creation = ledger.create_card(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    session_id=uuid.uuid4(),
                    parse=parse,
                    asr_text="进了十瓶矿泉水",
                    asr_confidence=0.95,
                    lines=[
                        {
                            "product_id": None,
                            "product_label": "矿泉水",
                            "quantity": 10,
                            "unit": "瓶",
                            "qty_base": 10,
                            "unit_price_fen": None,
                            "amount_fen": None,
                        }
                    ],
                    preview=PreviewBundle(),
                )
                out1 = ledger.confirm(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    card_id=uuid.UUID(creation.card_id),
                    confirm_type="button",
                    button_fallback=True,
                )
                assert out1.confirmed

            with db.session(bypass_scope=True) as session:
                prod = session.execute(
                    select(Product).where(Product.tenant_id == tenant_id, Product.name == "矿泉水")
                ).scalar_one()
                prod_id = prod.product_id
                assert prod.status == "draft"
                assert prod.last_purchase_price_fen is None
                assert prod.avg_cost_fen is None
                gap = session.execute(
                    select(Gap).where(Gap.tenant_id == tenant_id, Gap.kind == "missing_cost")
                ).scalar_one()
                assert gap.status == "open"

            # 2) 再以 100 分/瓶进货 10 瓶 -> 均价应为 100，而非 (0*10+100*10)/20 = 50
            with db.session(context=ctx) as session:
                ledger = LedgerService(session)
                parse2 = parse_of(
                    "purchase.create",
                    slots={"P": "矿泉水", "Q": 10, "U": "瓶", "UP": 100, "TA": 1000},
                )
                creation2 = ledger.create_card(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    session_id=uuid.uuid4(),
                    parse=parse2,
                    asr_text="进了十瓶矿泉水，一瓶一元",
                    asr_confidence=0.95,
                    lines=[
                        {
                            "product_id": str(prod_id),
                            "product_label": "矿泉水",
                            "quantity": 10,
                            "unit": "瓶",
                            "qty_base": 10,
                            "unit_price_fen": 100,
                            "amount_fen": 1000,
                        }
                    ],
                    preview=PreviewBundle(),
                )
                out2 = ledger.confirm(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    card_id=uuid.UUID(creation2.card_id),
                    confirm_type="button",
                    button_fallback=True,
                )
                assert out2.confirmed

            with db.session(bypass_scope=True) as session:
                prod = session.get(Product, prod_id)
                assert prod is not None and prod.avg_cost_fen == 100

            # 3) 补齐进价和售价 -> draft 自动变 active，missing_cost 待处理关闭
            patched = client.patch(
                f"/api/v1/products/{prod_id}",
                json={"last_purchase_price_fen": 100, "last_sale_price_fen": 200},
                headers=headers,
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["data"]["product"]["status"] == "active"

            ugaps = client.get("/api/v1/products/unified-gaps", headers=headers)
            assert ugaps.status_code == 200
            assert ugaps.json()["data"]["items"] == []
    finally:
        db.drop_all()
        db.dispose()


def test_multiline_same_product_and_credit_arap_payment_and_one_step_correct(seed) -> None:  # noqa: F811
    """验证：① 同单多行同商品合并扣批不重扣；② 赊账真 doc_id + 收款落 Payment；③ 超 10 分钟一步原子改单。"""
    t0 = datetime.now(timezone.utc)
    # 启用批次跟踪 + 先进 15 瓶可乐（100 分/瓶）
    with seed.database.session(bypass_scope=True) as session:
        prod = session.get(Product, seed.product_id)
        assert prod is not None
        prod.track_batch = True
        prod.cost_method = "batch"

    with seed.database.session(context=seed.context) as session:
        _, p_card = create_card_for(
            seed,
            session=session,
            intent_id="purchase.create",
            slots={"P": "可乐", "Q": 15, "U": "瓶", "UP": 100, "TA": 1500, "PS": "已结"},
            lines=[
                {
                    "product_id": str(seed.product_id),
                    "product_label": "可乐",
                    "quantity": 15,
                    "unit": "瓶",
                    "qty_base": 15,
                    "unit_price_fen": 100,
                    "amount_fen": 1500,
                }
            ],
            now=t0,
        )
        assert LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(p_card.card_id),
            confirm_type="button",
            now=t0 + timedelta(seconds=1),
        ).confirmed

    # 同一销售单含两行可乐（6 瓶 + 6 瓶）且赊账给「老王」
    with seed.database.session(context=seed.context) as session:
        _, s_card = create_card_for(
            seed,
            session=session,
            intent_id="sales.create_multi",
            slots={"P": "可乐", "Q": 12, "U": "瓶", "C": "老王", "PS": "未结", "TA": 3600},
            lines=[
                {
                    "product_id": str(seed.product_id),
                    "product_label": "可乐",
                    "quantity": 6,
                    "unit": "瓶",
                    "qty_base": 6,
                    "unit_price_fen": 300,
                    "amount_fen": 1800,
                },
                {
                    "product_id": str(seed.product_id),
                    "product_label": "可乐",
                    "quantity": 6,
                    "unit": "瓶",
                    "qty_base": 6,
                    "unit_price_fen": 300,
                    "amount_fen": 1800,
                },
            ],
            now=t0 + timedelta(seconds=10),
        )
        sale_out = LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(s_card.card_id),
            confirm_type="button",
            now=t0 + timedelta(seconds=11),
        )
        assert sale_out.confirmed
        sale_doc_id = uuid.UUID(str(sale_out.doc_id))

    with seed.database.session(bypass_scope=True) as session:
        batch = session.execute(
            select(StockBatch).where(StockBatch.tenant_id == seed.tenant_id)
        ).scalar_one()
        assert Decimal(batch.qty_left) == Decimal("3")  # 15 - (6+6) = 3
        inv = session.execute(
            select(Inventory).where(Inventory.tenant_id == seed.tenant_id, Inventory.product_id == seed.product_id)
        ).scalar_one()
        assert Decimal(inv.qty_base) == seed.qty_base + Decimal("3")  # 192 + 15 - (6+6)
        ar = session.execute(
            select(ArAp).where(ArAp.tenant_id == seed.tenant_id, ArAp.doc_id == sale_doc_id)
        ).scalar_one()
        assert int(ar.amount_fen) == 3600

    # 收款 2000 分 -> 落 Payment + 核销 2000
    with seed.database.session(context=seed.context) as session:
        _, pay_card = create_card_for(
            seed,
            session=session,
            intent_id="payment.receive",
            slots={"C": "老王", "AMT": 2000},
            lines=[
                {
                    "product_id": str(seed.product_id),
                    "product_label": "收款",
                    "quantity": 1,
                    "unit": "笔",
                    "qty_base": 1,
                    "unit_price_fen": 2000,
                    "amount_fen": 2000,
                }
            ],
            now=t0 + timedelta(seconds=20),
        )
        pay_out = LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(pay_card.card_id),
            confirm_type="button",
            button_fallback=True,
            now=t0 + timedelta(seconds=21),
        )
        assert pay_out.confirmed

    with seed.database.session(bypass_scope=True) as session:
        ar = session.execute(
            select(ArAp).where(ArAp.tenant_id == seed.tenant_id, ArAp.doc_id == sale_doc_id)
        ).scalar_one()
        assert int(ar.settled_amount_fen) == 2000
        pay = session.execute(
            select(Payment).where(Payment.tenant_id == seed.tenant_id, Payment.ar_ap_id == ar.ar_ap_id)
        ).scalar_one()
        assert int(pay.amount_fen) == 2000


def test_one_step_correct_http_and_count_mid_movements() -> None:
    """验证 HTTP 一步原子改单（超 10 分钟不拦）+ 盘点期间流水回补与盘盈新建独立批次。"""
    app = _make_app()
    db = app.state.database
    try:
        with TestClient(app) as client:
            headers, info = _login_headers(client, "boss-m0-http")
            tenant_id = uuid.UUID(info["tenant_id"])

            assert client.patch(
                "/api/v1/inventory/policy",
                json={"cost_method": "batch"},
                headers=headers,
            ).status_code == 200

            # 进 10 件可乐 × 8 元
            s1 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=headers)
            p1 = client.post(
                f"/api/v1/voice/sessions/{s1.json()['data']['session_id']}/parse",
                json={"asr_text": "进了十件可乐，一件八元"},
                headers=headers,
            )
            c1 = client.post(
                f"/api/v1/voice/cards/{p1.json()['data']['card']['meta']['card_id']}/confirm",
                json={"confirm_type": "button", "button_fallback": True},
                headers=headers,
            )
            doc_id = c1.json()["data"]["result"]["doc_id"]

            # 人为把单据时间调回 1 小时前，验证一步改单不受 10 分钟窗口限制
            with db.session(bypass_scope=True) as session:
                session.execute(
                    update(DocHeader)
                    .where(DocHeader.doc_id == uuid.UUID(doc_id))
                    .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
                )
                prod = session.execute(
                    select(Product).where(Product.tenant_id == tenant_id, Product.name == "可乐")
                ).scalar_one()
                prod_id = prod.product_id

            corrected = client.post(
                f"/api/v1/docs/{doc_id}/correct",
                json={
                    "lines": [
                        {
                            "product_id": str(prod_id),
                            "qty": 12,
                            "unit": "件",
                            "unit_price_fen": 800,
                        }
                    ],
                    "reason": "数错了，实进12件",
                },
                headers=headers,
            )
            assert corrected.status_code == 200, corrected.text
            res = corrected.json()["data"]["result"]
            new_doc_id = uuid.UUID(res["new_doc_id"])

            with db.session(bypass_scope=True) as session:
                orig_doc = session.get(DocHeader, uuid.UUID(doc_id))
                new_doc = session.get(DocHeader, new_doc_id)
                assert orig_doc is not None and orig_doc.status == "reversed"
                assert new_doc is not None and new_doc.corrects_doc_id == uuid.UUID(doc_id)
                assert int(new_doc.total_amount_fen) == 9600
                audits = list(
                    session.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant_id, AuditLog.action == "doc.correct"
                        )
                    ).scalars()
                )
                assert len(audits) == 1

            # 开盘点（账面 12 件），盘点期间卖了 2 件（实时余额 10 件），实盘 14 件 -> 应补 +4 件并新建盘盈批
            started = client.post("/api/v1/counts", headers=headers)
            count_id = started.json()["data"]["count_id"]

            s2 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=headers)
            p2 = client.post(
                f"/api/v1/voice/sessions/{s2.json()['data']['session_id']}/parse",
                json={"asr_text": "卖了两件可乐，一件十二元"},
                headers=headers,
            )
            assert (
                client.post(
                    f"/api/v1/voice/cards/{p2.json()['data']['card']['meta']['card_id']}/confirm",
                    json={"confirm_type": "button", "button_fallback": True},
                    headers=headers,
                ).status_code
                == 200
            )

            assert (
                client.patch(
                    f"/api/v1/counts/{count_id}/lines/{prod_id}",
                    json={"qty_counted": "14", "reason": "surplus"},
                    headers=headers,
                ).status_code
                == 200
            )
            applied = client.post(f"/api/v1/counts/{count_id}/apply", headers=headers)
            assert applied.status_code == 200, applied.text
            assert Decimal(applied.json()["data"]["result"]["total_qty_diff"]) == Decimal("4")

            # 已应用的盘点单禁止再改实盘行
            assert (
                client.patch(
                    f"/api/v1/counts/{count_id}/lines/{prod_id}",
                    json={"qty_counted": "9"},
                    headers=headers,
                ).status_code
                != 200
            )

            with db.session(bypass_scope=True) as session:
                inv = session.execute(
                    select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == prod_id)
                ).scalar_one()
                assert Decimal(inv.qty_base) == Decimal("14")
                surplus_batch = session.execute(
                    select(StockBatch).where(
                        StockBatch.tenant_id == tenant_id, StockBatch.label == "盘盈调整"
                    )
                ).scalar_one()
                assert Decimal(surplus_batch.qty_in) == Decimal("4")

            assert client.get("/api/v1/inventory/reconcile", headers=headers).json()["data"]["ok"] is True
    finally:
        db.drop_all()
        db.dispose()


def test_rbac_cost_visibility_disabled_member_and_deleting_export() -> None:
    """权限矩阵：readonly/clerk 越权拒、店员仅可撤自己当日单、成本默认隐藏、停用员工拦、注销冷静期可导出。"""
    app = _make_app()
    db = app.state.database
    try:
        with TestClient(app) as client:
            boss_headers, info = _login_headers(client, "boss-rbac-1")
            tenant_id = uuid.UUID(info["tenant_id"])
            store_id = uuid.UUID(info["store_id"])

            clerk_id, clerk_headers = _add_member_and_token(app, tenant_id, store_id, "wx-clerk-1", "clerk")
            _, ro_headers = _add_member_and_token(app, tenant_id, store_id, "wx-ro-1", "readonly")

            # 1) 老板进一笔货
            sb = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=boss_headers)
            pb = client.post(
                f"/api/v1/voice/sessions/{sb.json()['data']['session_id']}/parse",
                json={"asr_text": "进了五件可乐，一件八元"},
                headers=boss_headers,
            )
            boss_card_id = pb.json()["data"]["card"]["meta"]["card_id"]
            cb = client.post(
                f"/api/v1/voice/cards/{boss_card_id}/confirm",
                json={"confirm_type": "button", "button_fallback": True},
                headers=boss_headers,
            )
            boss_doc_id = cb.json()["data"]["result"]["doc_id"]

            # 2) readonly 取消卡片 → 403；clerk 访问 tasks / exports / policy / reconcile → 403
            sc = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=boss_headers)
            pc = client.post(
                f"/api/v1/voice/sessions/{sc.json()['data']['session_id']}/parse",
                json={"asr_text": "进了一件可乐，一件八元"},
                headers=boss_headers,
            )
            cancel_card_id = pc.json()["data"]["card"]["meta"]["card_id"]
            assert client.post(f"/api/v1/voice/cards/{cancel_card_id}/cancel", headers=ro_headers).status_code == 403

            assert client.get("/api/v1/tasks", headers=clerk_headers).status_code == 403
            assert client.get("/api/v1/exports", headers=clerk_headers).status_code == 403
            assert (
                client.patch("/api/v1/inventory/policy", json={"allow_negative": False}, headers=clerk_headers).status_code
                == 403
            )
            assert client.get("/api/v1/inventory/reconcile", headers=clerk_headers).status_code == 403

            # 3) clerk 撤老板单 → 403；店员自己今天开的卖货单 → 允许撤
            assert (
                client.post(f"/api/v1/docs/{boss_doc_id}/reversal", json={"reason": "test"}, headers=clerk_headers).status_code
                == 403
            )
            scl = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.4}, headers=clerk_headers)
            pcl = client.post(
                f"/api/v1/voice/sessions/{scl.json()['data']['session_id']}/parse",
                json={"asr_text": "卖了一件可乐，一件十二元"},
                headers=clerk_headers,
            )
            ccl = client.post(
                f"/api/v1/voice/cards/{pcl.json()['data']['card']['meta']['card_id']}/confirm",
                json={"confirm_type": "button", "button_fallback": True},
                headers=clerk_headers,
            )
            clerk_doc_id = ccl.json()["data"]["result"]["doc_id"]

            # 4) 默认关闭 staff_can_view_cost：店员看不到进价、采购额、毛利
            rep_clerk = client.get("/api/v1/query/report", headers=clerk_headers).json()["data"]
            assert rep_clerk["cost_hidden"] is True
            assert rep_clerk["report"]["purchase_fen"] is None
            assert rep_clerk["report"]["gross_profit_fen"] is None
            stk_clerk = client.get("/api/v1/query/stock", headers=clerk_headers).json()["data"]
            assert stk_clerk["cost_hidden"] is True
            assert stk_clerk["items"][0]["last_purchase_price_fen"] is None

            # 老板放开开关后，店员可见
            assert (
                client.patch("/api/v1/inventory/policy", json={"staff_can_view_cost": True}, headers=boss_headers).status_code
                == 200
            )
            rep_clerk2 = client.get("/api/v1/query/report", headers=clerk_headers).json()["data"]
            assert rep_clerk2["cost_hidden"] is False
            assert rep_clerk2["report"]["gross_profit_fen"] == 400

            # 店员撤自己当日单 → 200；top_products 只计销不计进
            assert (
                client.post(f"/api/v1/docs/{clerk_doc_id}/reversal", json={"reason": "误按"}, headers=clerk_headers).status_code
                == 200
            )
            top = client.get("/api/v1/query/report?top=true", headers=boss_headers).json()["data"]["top_products"]
            assert top == []

            # 5) 停用店员 → 已有 JWT 被拦截 + 重新登录也被拦截
            with db.session(bypass_scope=True) as session:
                m = session.get(Member, clerk_id)
                assert m is not None
                m.status = "disabled"
            assert client.get("/api/v1/query/stock", headers=clerk_headers).status_code == 403
            assert client.post("/api/v1/auth/login", json={"code": "wx-clerk-1"}).status_code == 403

            # 6) 注销冷静期（DeletionRequest=pending 与 Tenant.status=deleting）内老板仍可发起数据导出，不拿数据当人质
            with db.session(bypass_scope=True) as session:
                session.add(
                    DeletionRequest(
                        tenant_id=tenant_id,
                        status="pending",
                        reason="测试冷静期导出",
                    )
                )
            # 冷静期内写业务动作被冻结，但导出允许
            assert (
                client.patch("/api/v1/inventory/policy", json={"allow_negative": False}, headers=boss_headers).status_code
                == 403
            )
            exp1 = client.post(
                "/api/v1/exports",
                json={"scope": "docs", "file_format": "csv"},
                headers=boss_headers,
            )
            assert exp1.status_code == 200, exp1.text

            with db.session(bypass_scope=True) as session:
                t = session.get(Tenant, tenant_id)
                assert t is not None
                t.status = "deleting"
            exp2 = client.post(
                "/api/v1/exports",
                json={"scope": "stock", "file_format": "csv"},
                headers=boss_headers,
            )
            assert exp2.status_code == 200, exp2.text
    finally:
        db.drop_all()
        db.dispose()
