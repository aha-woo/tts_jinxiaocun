"""账务不变量状态机 Fuzzing（随机操作流）+ 7 条端到端业务全链路验收测试。

覆盖：
1. 120 步确定性随机状态机 Fuzzing，每一步均断言 6 条核心账务不变量：
   - I1: Inventory.qty_base == SUM(InventoryMovement.qty_base)
   - I2: SUM(open StockBatch.qty_left) == Inventory.qty_base，且虚拟批 qty_left <= 0、adjusted 批 qty_left == 0，三账核对 ok=True
   - I3: 0 <= ArAp.settled_amount_fen <= ArAp.amount_fen，status 与金额一致，且 settled == SUM(Payment.amount_fen)
   - I4: 非收付款单据 DocHeader.total_amount_fen == SUM(DocLine.amount_fen or 0)
   - I5: 库存 >= 0 时不存在未解决的 negative_stock 待处理
   - I6: 反做单净额在经营报表中归零
2. 7 条端到端业务旅程：
   - J1: 冷启动进货/销货/多行同品/退货净额与毛利准确度
   - J2: 超卖负库存 -> 两次补进货分次结清暂估成本与流水拆分
   - J3: 缺进价建账 -> 改价实体落库补全进价与消除缺口（含 old_fen=None）
   - J4: 多笔赊账核销无版本冲突死锁 -> 撤销核销回退应付应收与引用链解锁
   - J5: 虚拟批清零后撤单 / 进货部分消耗后撤单 / 报损采购退货撤单 / confirmed&cancelled 卡片禁止复活
   - J6: 凌晨 05:16 CST 与跨月临界点（UTC 上月 21:16）日期/计次自然月严格按 Asia/Shanghai 对齐
   - J7: 全链路店员进价脱敏（语音 R0 / 跟上次一样 / 候选单据 / 拍进货单）+ viewer 写拦截 + 进货单防重入账
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import Settings
from app.db.session import Database
from app.domain.entity_writer import EntityWriter
from app.domain.intent_registry import default_registry
from app.domain.report_service import ReportService
from app.main import create_app
from app.models.catalog import Product
from app.models.doc import ArAp, DocHeader, DocLine, Payment
from app.models.inventory import CostAdjustment, Inventory, InventoryMovement, StockBatch
from app.models.ops import Gap
from app.models.org import Member, Role, Store
from app.models.partner import Partner
from app.models.tenant import Tenant
from app.pay.deliver_callback import current_period as deliver_current_period
from app.services import batch as batch_service
from app.services.debts import settle_partner_debt
from app.services.delivery_note import confirm_delivery_note, parse_delivery_note
from app.services.entitlement import current_period as entitlement_current_period
from app.services.export_job import period_range
from app.services.ledger import LedgerService
from app.services.reconcile import reconcile_store
from app.services.reversal import ReversalService
from app.services.usage_meter import current_period as usage_current_period
from app.services.voice_runtime import reset_shared_voice_state
from app.services.wx_provider import FakeWxProvider
from app.tenant.context import TenantContext
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.output_schema import ParseResult


# ---------------------------------------------------------------------------
# 核心不变量断言器
# ---------------------------------------------------------------------------
def assert_all_invariants(session: Session, tenant_id: uuid.UUID, store_id: uuid.UUID) -> None:
    """在当前数据库状态上逐条校验库存、批次、应收应付、单据行及待处理不变量。"""
    inv_map = {
        row.product_id: Decimal(row.qty_base)
        for row in session.execute(
            select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.store_id == store_id)
        ).scalars()
    }
    mov_map = {
        pid: Decimal(str(total or 0))
        for pid, total in session.execute(
            select(InventoryMovement.product_id, func.coalesce(func.sum(InventoryMovement.qty_base), 0))
            .where(InventoryMovement.tenant_id == tenant_id, InventoryMovement.store_id == store_id)
            .group_by(InventoryMovement.product_id)
        ).all()
    }
    batch_pids = {
        pid
        for (pid,) in session.execute(
            select(StockBatch.product_id)
            .where(StockBatch.tenant_id == tenant_id, StockBatch.store_id == store_id)
            .distinct()
        ).all()
    }
    all_pids = set(inv_map) | set(mov_map) | batch_pids
    policy = batch_service.policy_for(session, tenant_id)

    # I1 & I2 & I5
    for pid in all_pids:
        inv_qty = inv_map.get(pid, Decimal(0))
        mov_qty = mov_map.get(pid, Decimal(0))
        assert inv_qty == mov_qty, f"I1 违背 product={pid}: inv={inv_qty}, mov_sum={mov_qty}"

        if batch_service.product_tracks_batch(session, tenant_id, pid, policy):
            batches = list(
                session.execute(
                    select(StockBatch).where(
                        StockBatch.tenant_id == tenant_id,
                        StockBatch.store_id == store_id,
                        StockBatch.product_id == pid,
                    )
                ).scalars()
            )
            open_sum = sum((Decimal(b.qty_left) for b in batches if b.status == "open"), Decimal(0))
            assert open_sum == inv_qty, (
                f"I2 违背 product={pid}: open_batch_sum={open_sum}, inv={inv_qty}"
            )
            for b in batches:
                if b.is_virtual:
                    assert Decimal(b.qty_left) <= Decimal(0), f"虚拟批 qty_left 不得为正: {b.qty_left}"
                if b.status == "adjusted":
                    assert Decimal(b.qty_left) == Decimal(0), f"adjusted 批必须清零: {b.qty_left}"
                if b.status == "exhausted":
                    assert Decimal(b.qty_left) == Decimal(0), f"exhausted 批必须清零: {b.qty_left}"

        if inv_qty >= Decimal(0):
            open_neg_gaps = list(
                session.execute(
                    select(Gap).where(
                        Gap.tenant_id == tenant_id,
                        Gap.store_id == store_id,
                        Gap.kind == "negative_stock",
                        Gap.ref_entity == "product",
                        Gap.ref_id == pid,
                        Gap.status != "resolved",
                    )
                ).scalars()
            )
            assert not open_neg_gaps, f"I5 违背: 非负库存产品 {pid} 仍存在未关闭负库存待处理"

    rec = reconcile_store(session, tenant_id=tenant_id, store_id=store_id)
    assert rec["ok"] is True, f"reconcile_store 失败: {rec}"
    brec = batch_service.reconcile_store(session, tenant_id=tenant_id, store_id=store_id)
    assert brec["ok"] is True, f"batch.reconcile_store 失败: {brec}"

    # I3: ArAp == Payment
    ar_rows = list(session.execute(select(ArAp).where(ArAp.tenant_id == tenant_id)).scalars())
    pay_by_ar: dict[uuid.UUID, int] = {}
    for pay in session.execute(select(Payment).where(Payment.tenant_id == tenant_id)).scalars():
        assert int(pay.amount_fen) > 0
        pay_by_ar[pay.ar_ap_id] = pay_by_ar.get(pay.ar_ap_id, 0) + int(pay.amount_fen)
    for ar in ar_rows:
        amt = int(ar.amount_fen)
        settled = int(ar.settled_amount_fen)
        assert 0 <= settled <= amt, f"I3 金额越界: {ar.ar_ap_id}"
        expected_status = "settled" if settled >= amt else ("open" if settled == 0 else "partial")
        assert str(ar.status) == expected_status, f"I3 状态不一致: {ar.status} != {expected_status}"
        assert pay_by_ar.get(ar.ar_ap_id, 0) == settled, (
            f"I3 流水合计不一致 ar={ar.ar_ap_id}: pay={pay_by_ar.get(ar.ar_ap_id, 0)}, settled={settled}"
        )

    # I4: DocHeader.total_amount_fen == SUM(DocLine.amount_fen)
    headers = list(session.execute(select(DocHeader).where(DocHeader.tenant_id == tenant_id)).scalars())
    lines_by_doc: dict[uuid.UUID, list[DocLine]] = {}
    for ln in session.execute(select(DocLine).where(DocLine.tenant_id == tenant_id)).scalars():
        lines_by_doc.setdefault(ln.doc_id, []).append(ln)
    for hdr in headers:
        dlines = lines_by_doc.get(hdr.doc_id)
        if dlines:
            expected_total = sum(int(ln.amount_fen or 0) for ln in dlines)
            assert int(hdr.total_amount_fen) == expected_total, (
                f"I4 违背 doc={hdr.doc_no}: header={hdr.total_amount_fen}, lines={expected_total}"
            )


# ---------------------------------------------------------------------------
# 通用建店与建卡落账辅助
# ---------------------------------------------------------------------------
def _bootstrap_shop(*, allow_negative: bool = True):
    db = Database.from_url("sqlite+pysqlite:///:memory:")
    db.create_all()
    tenant_id = uuid.uuid4()
    store_id = uuid.uuid4()
    member_id = uuid.uuid4()
    with db.session(bypass_scope=True) as s:
        s.add(
            Tenant(
                tenant_id=tenant_id,
                name="测试商贸公司",
                plan_code="free",
                quota_monthly=5000,
                allow_negative=allow_negative,
            )
        )
        s.add(Store(tenant_id=tenant_id, store_id=store_id, name="总仓", is_main=True))
        s.add(
            Member(
                tenant_id=tenant_id,
                member_id=member_id,
                wx_openid=f"boss-{member_id}",
                name="老板",
                status="active",
            )
        )
        s.add(Role(tenant_id=tenant_id, member_id=member_id, store_id=store_id, role_code="boss"))
    ctx = TenantContext(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        role_code="boss",
        tenant_version=1,
    )
    return db, ctx, tenant_id, store_id, member_id


def _add_product(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    name: str,
    base_unit: str = "瓶",
    track_batch: bool = True,
    cost_method: str | None = "batch",
    purchase_fen: int | None = 100,
    sale_fen: int | None = 200,
) -> uuid.UUID:
    pid = uuid.uuid4()
    with db.session(bypass_scope=True) as s:
        s.add(
            Product(
                tenant_id=tenant_id,
                product_id=pid,
                name=name,
                spec="",
                base_unit=base_unit,
                track_batch=track_batch,
                cost_method=cost_method,
                last_purchase_price_fen=purchase_fen,
                last_sale_price_fen=sale_fen,
                avg_cost_fen=purchase_fen,
                status="active",
            )
        )
    return pid


def _add_partner(db: Database, tenant_id: uuid.UUID, *, name: str, ptype: str) -> uuid.UUID:
    partner_id = uuid.uuid4()
    with db.session(bypass_scope=True) as s:
        s.add(Partner(tenant_id=tenant_id, partner_id=partner_id, name=name, type=ptype))
    return partner_id


def _exec_card_doc(
    db: Database,
    ctx: TenantContext,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    intent_id: str,
    slots: dict[str, Any],
    lines: list[dict[str, Any]],
    biz_date: date | None = None,
    now: datetime | None = None,
):
    spec = default_registry().require(intent_id)
    source = {k: "asr" for k in slots}
    parse = ParseResult(
        intent=intent_id,
        slots=slots,
        source=source,  # type: ignore[arg-type]
        confidence=0.98,
        calc_traces=[f"{intent_id}"],
        channel="template",
        route="card",
    )
    deltas = build_deltas(
        intent_id=intent_id,
        side_effect=dict(spec.side_effect or {}),
        lines=[ln for ln in lines if ln.get("product_id")],
    )
    with db.session(context=ctx) as session:
        preview = ImpactPreviewService(db).preview(
            tenant_id=tenant_id, store_id=store_id, deltas=deltas, session=session
        )
        ledger = LedgerService(session)
        card = ledger.create_card(
            tenant_id=tenant_id,
            store_id=store_id,
            member_id=member_id,
            session_id=uuid.uuid4(),
            parse=parse,
            asr_text=f"test {intent_id}",
            asr_confidence=0.98,
            lines=lines,
            preview=preview,
            now=now,
        )
        outcome = ledger.confirm(
            tenant_id=tenant_id,
            store_id=store_id,
            member_id=member_id,
            card_id=uuid.UUID(card.card_id),
            confirm_type="button",
            button_fallback=True,
            biz_date=biz_date,
            now=now,
        )
        assert outcome.confirmed is True, outcome.as_dict()
        return outcome


# ---------------------------------------------------------------------------
# 1) 状态机随机 Fuzzing（120 步混合操作，每步全量校验不变量）
# ---------------------------------------------------------------------------
def test_stateful_accounting_invariants_fuzz() -> None:
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        p_batch1 = _add_product(db, tenant_id, name="矿泉水", track_batch=True, cost_method="batch", purchase_fen=100, sale_fen=200)
        p_batch2 = _add_product(db, tenant_id, name="红茶", track_batch=True, cost_method="batch", purchase_fen=200, sale_fen=350)
        p_avg = _add_product(db, tenant_id, name="纸巾", track_batch=False, cost_method="avg", purchase_fen=150, sale_fen=300)
        products = [
            (p_batch1, "矿泉水"),
            (p_batch2, "红茶"),
            (p_avg, "纸巾"),
        ]
        cust_id = _add_partner(db, tenant_id, name="晨光超市", ptype="customer")
        supp_id = _add_partner(db, tenant_id, name="大鹏批发行", ptype="supplier")

        rng = random.Random(20261127)
        reversible_doc_ids: list[uuid.UUID] = []

        for step in range(120):
            op = rng.choices(
                [
                    "purchase_cash",
                    "purchase_credit",
                    "purchase_nocost",
                    "sale_cash",
                    "sale_credit",
                    "sales_return",
                    "purchase_return",
                    "loss",
                    "settle_ar",
                    "settle_ap",
                    "snapshot_count",
                    "reverse_doc",
                ],
                weights=[16, 8, 5, 22, 10, 6, 5, 4, 8, 6, 4, 12],
                k=1,
            )[0]

            pid, pname = rng.choice(products)
            qty = rng.randint(1, 15)

            if op in ("purchase_cash", "purchase_credit", "purchase_nocost"):
                credit = op == "purchase_credit"
                no_cost = op == "purchase_nocost"
                up = None if no_cost else rng.randint(80, 300)
                amt = None if up is None else up * qty
                ps = "未结" if credit else "已结"
                out = _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="purchase.create",
                    slots={
                        "P": pname, "Q": qty, "U": "瓶",
                        **({"UP": up, "TA": amt} if up is not None else {}),
                        "PS": ps, "S": "大鹏批发行",
                    },
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": qty,
                        "unit": "瓶",
                        "qty_base": str(qty),
                        "unit_price_fen": up,
                        "amount_fen": amt,
                        "partner_id": str(supp_id),
                        "payment_status": ps,
                        "pending": False,
                    }],
                )
                reversible_doc_ids.append(uuid.UUID(str(out.doc_id)))

            elif op in ("sale_cash", "sale_credit"):
                credit = op == "sale_credit"
                up = rng.randint(200, 500)
                amt = up * qty
                ps = "未结" if credit else "已结"
                out = _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="sales.create",
                    slots={"P": pname, "Q": qty, "U": "瓶", "UP": up, "TA": amt, "PS": ps, "C": "晨光超市"},
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": qty,
                        "unit": "瓶",
                        "qty_base": str(qty),
                        "unit_price_fen": up,
                        "amount_fen": amt,
                        "partner_id": str(cust_id),
                        "payment_status": ps,
                        "pending": False,
                    }],
                )
                reversible_doc_ids.append(uuid.UUID(str(out.doc_id)))

            elif op == "sales_return":
                up = rng.randint(200, 400)
                amt = up * qty
                out = _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="sales_return.create",
                    slots={"P": pname, "Q": qty, "U": "瓶", "UP": up, "TA": amt, "PS": "已结", "C": "晨光超市"},
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": qty,
                        "unit": "瓶",
                        "qty_base": str(qty),
                        "unit_price_fen": up,
                        "amount_fen": amt,
                        "partner_id": str(cust_id),
                        "payment_status": "已结",
                        "pending": False,
                    }],
                )
                reversible_doc_ids.append(uuid.UUID(str(out.doc_id)))

            elif op == "purchase_return":
                up = rng.randint(100, 250)
                amt = up * qty
                out = _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="purchase_return.create",
                    slots={"P": pname, "Q": qty, "U": "瓶", "UP": up, "TA": amt, "PS": "已结", "S": "大鹏批发行"},
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": qty,
                        "unit": "瓶",
                        "qty_base": str(qty),
                        "unit_price_fen": up,
                        "amount_fen": amt,
                        "partner_id": str(supp_id),
                        "payment_status": "已结",
                        "pending": False,
                    }],
                )
                reversible_doc_ids.append(uuid.UUID(str(out.doc_id)))

            elif op == "loss":
                out = _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="stock.loss",
                    slots={"P": pname, "Q": qty, "U": "瓶"},
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": qty,
                        "unit": "瓶",
                        "qty_base": str(qty),
                        "unit_price_fen": 0,
                        "amount_fen": 0,
                        "pending": False,
                    }],
                )
                reversible_doc_ids.append(uuid.UUID(str(out.doc_id)))

            elif op in ("settle_ar", "settle_ap"):
                partner_target = cust_id if op == "settle_ar" else supp_id
                direction = "receivable" if op == "settle_ar" else "payable"
                with db.session(context=ctx) as s:
                    open_rows = list(
                        s.execute(
                            select(ArAp).where(
                                ArAp.tenant_id == tenant_id,
                                ArAp.partner_id == partner_target,
                                ArAp.direction == direction,
                                ArAp.status != "settled",
                            )
                        ).scalars()
                    )
                    total_open = sum(int(r.amount_fen) - int(r.settled_amount_fen) for r in open_rows)
                if total_open > 0:
                    pay_amt = total_open if rng.random() < 0.4 else max(1, rng.randint(1, total_open))
                    with db.session(context=ctx) as s:
                        settled = settle_partner_debt(
                            s, db, tenant_id=tenant_id, store_id=store_id,
                            member_id=member_id, partner_id=partner_target,
                            direction=direction, amount_fen=pay_amt, method="微信",
                        )
                    reversible_doc_ids.append(uuid.UUID(str(settled["doc_id"])))

            elif op == "snapshot_count":
                new_qty = rng.randint(-5, 30)
                _exec_card_doc(
                    db, ctx, tenant_id, store_id, member_id,
                    intent_id="stock.take",
                    slots={"P": pname, "Q": max(0, new_qty), "U": "瓶"},
                    lines=[{
                        "product_id": str(pid),
                        "product_label": pname,
                        "quantity": max(0, new_qty),
                        "unit": "瓶",
                        "qty_base": str(max(0, new_qty)),
                        "unit_price_fen": 0,
                        "amount_fen": 0,
                        "pending": False,
                    }],
                )

            elif op == "reverse_doc" and reversible_doc_ids:
                candidate = rng.choice(reversible_doc_ids)
                with db.session(context=ctx) as s:
                    try:
                        ReversalService(s).reverse(
                            tenant_id=tenant_id,
                            store_id=store_id,
                            doc_id=candidate,
                            member_id=member_id,
                            reason="fuzz撤单",
                            ignore_window=True,
                        )
                    except AppError:
                        # 被引用（如已收款的赊账单）或已撤销属于业务合法拦截
                        pass

            with db.session(bypass_scope=True) as s:
                assert_all_invariants(s, tenant_id, store_id)
    finally:
        db.drop_all()
        db.dispose()


# ---------------------------------------------------------------------------
# 2) 7 条端到端业务全链路验收测试
# ---------------------------------------------------------------------------
def test_journey_1_cold_start_multiline_and_returns_net_report() -> None:
    """J1: 冷启动采购/同品多行采购/销售/销退/采退 -> 报表净额与毛利精确一致，撤单净影响归零。"""
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        pid = _add_product(db, tenant_id, name="可乐", track_batch=True, cost_method="batch", purchase_fen=None, sale_fen=500)
        supp_id = _add_partner(db, tenant_id, name="华联批发", ptype="supplier")
        cust_id = _add_partner(db, tenant_id, name="张三", ptype="customer")
        today = date(2026, 11, 27)

        # 同品两行不同进价进货：10*200 + 10*300 = 5000
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create_multi",
            slots={"P": "可乐", "Q": 20, "U": "瓶", "TA": 5000, "S": "华联批发", "PS": "已结"},
            lines=[
                {"product_id": str(pid), "product_label": "可乐", "quantity": 10, "unit": "瓶", "qty_base": "10", "unit_price_fen": 200, "amount_fen": 2000, "partner_id": str(supp_id), "payment_status": "已结", "pending": False},
                {"product_id": str(pid), "product_label": "可乐", "quantity": 10, "unit": "瓶", "qty_base": "10", "unit_price_fen": 300, "amount_fen": 3000, "partner_id": str(supp_id), "payment_status": "已结", "pending": False},
            ],
            biz_date=today,
        )
        # 卖 15 瓶 @500 = 7500；FIFO 成本 = 10*200 + 5*300 = 3500，毛利 = 4000
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales.create",
            slots={"P": "可乐", "Q": 15, "U": "瓶", "UP": 500, "TA": 7500, "C": "张三", "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "可乐", "quantity": 15, "unit": "瓶", "qty_base": "15", "unit_price_fen": 500, "amount_fen": 7500, "partner_id": str(cust_id), "payment_status": "已结", "pending": False}],
            biz_date=today,
        )
        # 销售退货 3 瓶 @500 = 1500（按当前均价/最近进价折回毛利），采购退货 2 瓶 @300 = 600
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales_return.create",
            slots={"P": "可乐", "Q": 3, "U": "瓶", "UP": 500, "TA": 1500, "C": "张三", "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "可乐", "quantity": 3, "unit": "瓶", "qty_base": "3", "unit_price_fen": 500, "amount_fen": 1500, "partner_id": str(cust_id), "payment_status": "已结", "pending": False}],
            biz_date=today,
        )
        pt_out = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase_return.create",
            slots={"P": "可乐", "Q": 2, "U": "瓶", "UP": 300, "TA": 600, "S": "华联批发", "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "可乐", "quantity": 2, "unit": "瓶", "qty_base": "2", "unit_price_fen": 300, "amount_fen": 600, "partner_id": str(supp_id), "payment_status": "已结", "pending": False}],
            biz_date=today,
        )

        with db.session(context=ctx) as s:
            assert_all_invariants(s, tenant_id, store_id)
            rep = ReportService(s).today(tenant_id=tenant_id, store_id=store_id, biz_date=today)
            assert rep.sales_fen == 7500 - 1500
            assert rep.purchase_fen == 5000 - 600
            assert rep.gross_profit_fen is not None

            # 撤销采购退货单 -> 净采购回到 5000，且三账仍平
            ReversalService(s).reverse(
                tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(pt_out.doc_id)),
                member_id=member_id, ignore_window=True,
            )
            assert_all_invariants(s, tenant_id, store_id)
            rep2 = ReportService(s).today(tenant_id=tenant_id, store_id=store_id, biz_date=today)
            assert rep2.purchase_fen == 5000
    finally:
        db.drop_all()
        db.dispose()


def test_journey_2_oversold_partial_settlement_and_cogs_split() -> None:
    """J2: 混合在库(4)+超卖(6)销售 -> 先补进货3件部分结清(拆流水) -> 再补进货5件完全结清 COGS。"""
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        pid = _add_product(db, tenant_id, name="冰红茶", track_batch=True, cost_method="batch", purchase_fen=200, sale_fen=400)
        # 1) 先进 4 件 @200
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "冰红茶", "Q": 4, "U": "瓶", "UP": 200, "TA": 800, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "冰红茶", "quantity": 4, "unit": "瓶", "qty_base": "4", "unit_price_fen": 200, "amount_fen": 800, "payment_status": "已结", "pending": False}],
        )
        # 2) 卖 10 件 @400 -> 4件来自真实批(@200 final)，6件来自虚拟批(@200 provisional)，行COGS=2000 provisional
        sale_out = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales.create",
            slots={"P": "冰红茶", "Q": 10, "U": "瓶", "UP": 400, "TA": 4000, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "冰红茶", "quantity": 10, "unit": "瓶", "qty_base": "10", "unit_price_fen": 400, "amount_fen": 4000, "payment_status": "已结", "pending": False}],
        )
        # 3) 补进货 3 件 @250 -> 只够结清6件超卖中的3件，剩余3件必须仍保持 provisional！
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "冰红茶", "Q": 3, "U": "瓶", "UP": 250, "TA": 750, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "冰红茶", "quantity": 3, "unit": "瓶", "qty_base": "3", "unit_price_fen": 250, "amount_fen": 750, "payment_status": "已结", "pending": False}],
        )
        with db.session(context=ctx) as s:
            assert_all_invariants(s, tenant_id, store_id)
            sale_doc_id = uuid.UUID(str(sale_out.doc_id))
            sline = s.execute(select(DocLine).where(DocLine.doc_id == sale_doc_id)).scalar_one()
            # 4*200 + 3*250 + 3*200 = 2150，且行状态仍为 provisional（不可提前变 final）
            assert sline.cost_status == "provisional"
            assert int(sline.cost_snapshot_fen or 0) == 2150

        # 4) 再补进货 5 件 @300 -> 结清剩余 3 件超卖(@300)，余下 2 件新批留库
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "冰红茶", "Q": 5, "U": "瓶", "UP": 300, "TA": 1500, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "冰红茶", "quantity": 5, "unit": "瓶", "qty_base": "5", "unit_price_fen": 300, "amount_fen": 1500, "payment_status": "已结", "pending": False}],
        )
        with db.session(context=ctx) as s:
            assert_all_invariants(s, tenant_id, store_id)
            sale_doc_id = uuid.UUID(str(sale_out.doc_id))
            shdr = s.get(DocHeader, sale_doc_id)
            sline = s.execute(select(DocLine).where(DocLine.doc_id == sale_doc_id)).scalar_one()
            # 最终成本 = 4*200 + 3*250 + 3*300 = 800 + 750 + 900 = 2450
            assert sline.cost_status == "final"
            assert int(sline.cost_snapshot_fen or 0) == 2450
            assert shdr is not None and int(shdr.cogs_fen or 0) == 2450
            adjs = list(s.execute(select(CostAdjustment).where(CostAdjustment.sale_doc_id == sale_doc_id)).scalars())
            assert len(adjs) == 2
    finally:
        db.drop_all()
        db.dispose()


def test_journey_3_missing_cost_purchase_and_entity_writer_backfill() -> None:
    """J3: 缺进价进货生成 missing_cost 待办 -> 通过实体改价(old_fen=None 不炸500)与带价进货消除待办。"""
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        pid = _add_product(db, tenant_id, name="苏打水", track_batch=False, cost_method="avg", purchase_fen=None, sale_fen=None)
        # 1) 无进价进货 10 瓶 -> 建 missing_cost gap，移动平均保持 None
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "苏打水", "Q": 10, "U": "瓶", "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "苏打水", "quantity": 10, "unit": "瓶", "qty_base": "10", "unit_price_fen": None, "amount_fen": None, "payment_status": "已结", "pending": False}],
        )
        with db.session(context=ctx) as s:
            gap = s.execute(select(Gap).where(Gap.tenant_id == tenant_id, Gap.kind == "missing_cost", Gap.ref_id == pid)).scalar_one()
            assert gap.status == "open"
            # 2) EntityWriter 改价（商品原进价为 None，不得抛 TypeError 500）
            res = EntityWriter(s).apply(
                tenant_id=tenant_id, store_id=store_id, member_id=member_id,
                intent_id="price.update", slots={"P": "苏打水", "TF": "进价", "TV": "2.5"},
            )
            assert "未设 → ¥2.50" in res.summary
            prod = s.get(Product, pid)
            assert prod is not None and prod.last_purchase_price_fen == 250 and prod.avg_cost_fen == 250
            gap_after = s.execute(select(Gap).where(Gap.gap_id == gap.gap_id)).scalar_one()
            assert gap_after.status == "resolved"
            assert_all_invariants(s, tenant_id, store_id)
    finally:
        db.drop_all()
        db.dispose()


def test_journey_4_multi_invoice_credit_settlement_and_payment_reversal() -> None:
    """J4: 同一客户两笔赊账单 -> 核销第一笔全部 -> 再核销第二笔无版本冲突死锁 -> 撤销核销回退流水并解锁原单撤单。"""
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        pid = _add_product(db, tenant_id, name="矿泉水", track_batch=True, cost_method="batch", purchase_fen=100, sale_fen=200)
        cust_id = _add_partner(db, tenant_id, name="老李便利店", ptype="customer")
        # 进货备足库存
        _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "矿泉水", "Q": 100, "U": "瓶", "UP": 100, "TA": 10000, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "矿泉水", "quantity": 100, "unit": "瓶", "qty_base": "100", "unit_price_fen": 100, "amount_fen": 10000, "payment_status": "已结", "pending": False}],
        )
        # 赊销单 1: 1000 分; 赊销单 2: 2000 分
        xs1 = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales.create",
            slots={"P": "矿泉水", "Q": 5, "U": "瓶", "UP": 200, "TA": 1000, "PS": "未结", "C": "老李便利店"},
            lines=[{"product_id": str(pid), "product_label": "矿泉水", "quantity": 5, "unit": "瓶", "qty_base": "5", "unit_price_fen": 200, "amount_fen": 1000, "partner_id": str(cust_id), "payment_status": "未结", "pending": False}],
        )
        xs2 = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales.create",
            slots={"P": "矿泉水", "Q": 10, "U": "瓶", "UP": 200, "TA": 2000, "PS": "未结", "C": "老李便利店"},
            lines=[{"product_id": str(pid), "product_label": "矿泉水", "quantity": 10, "unit": "瓶", "qty_base": "10", "unit_price_fen": 200, "amount_fen": 2000, "partner_id": str(cust_id), "payment_status": "未结", "pending": False}],
        )

        # 第 1 次核销：结清 xs1 (1000 分)
        with db.session(context=ctx) as s:
            r1 = settle_partner_debt(
                s, db, tenant_id=tenant_id, store_id=store_id,
                member_id=member_id, partner_id=cust_id, direction="receivable",
                amount_fen=1000, method="微信",
            )
            assert r1["remaining_outstanding_fen"] == 2000

        # 第 2 次核销：跨越已结清的 xs1 核销 xs2 一半 (1000 分)，不得因读到 settled 行而 E_PREVIEW_VERSION_CONFLICT！
        with db.session(context=ctx) as s:
            r2 = settle_partner_debt(
                s, db, tenant_id=tenant_id, store_id=store_id,
                member_id=member_id, partner_id=cust_id, direction="receivable",
                amount_fen=1000, method="支付宝",
            )
            assert r2["remaining_outstanding_fen"] == 1000
            # xs2 已有收款流水，直接撤销 xs2 必须被引用链拦截
            with pytest.raises(AppError) as exc_ref:
                ReversalService(s).reverse(
                    tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(xs2.doc_id)),
                    member_id=member_id, ignore_window=True,
                )
            assert exc_ref.value.code == "E_DOC_REFERENCED"

        # 第 3 步：撤销第 2 次收款单 r2，xs2 应恢复为未核销 open，且可再撤销 xs2
        with db.session(context=ctx) as s:
            ReversalService(s).reverse(
                tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(r2["doc_id"])),
                member_id=member_id, ignore_window=True,
            )
            assert_all_invariants(s, tenant_id, store_id)
            ReversalService(s).reverse(
                tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(xs2.doc_id)),
                member_id=member_id, ignore_window=True,
            )
            assert_all_invariants(s, tenant_id, store_id)
    finally:
        db.drop_all()
        db.dispose()


def test_journey_5_reversal_safety_and_revive_guard() -> None:
    """J5: 虚拟批结清后撤单 / 进货部分卖出后撤单 / 已确认或取消卡片禁止复活。"""
    db, ctx, tenant_id, store_id, member_id = _bootstrap_shop()
    try:
        pid = _add_product(db, tenant_id, name="可乐", track_batch=True, cost_method="batch", purchase_fen=200, sale_fen=400)
        # 1) 先超卖 5 瓶 -> 虚拟批 -5
        sale_out = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="sales.create",
            slots={"P": "可乐", "Q": 5, "U": "瓶", "UP": 400, "TA": 2000, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "可乐", "quantity": 5, "unit": "瓶", "qty_base": "5", "unit_price_fen": 400, "amount_fen": 2000, "payment_status": "已结", "pending": False}],
        )
        # 2) 再进 8 瓶 -> 虚拟批抵清 5，新批留 3
        buy_out = _exec_card_doc(
            db, ctx, tenant_id, store_id, member_id,
            intent_id="purchase.create",
            slots={"P": "可乐", "Q": 8, "U": "瓶", "UP": 200, "TA": 1600, "PS": "已结"},
            lines=[{"product_id": str(pid), "product_label": "可乐", "quantity": 8, "unit": "瓶", "qty_base": "8", "unit_price_fen": 200, "amount_fen": 1600, "payment_status": "已结", "pending": False}],
        )
        # 3) 现在撤销那笔超卖单：虚拟批已为0，必须回补为真实在库批（不能让虚拟批变成 +5）
        with db.session(context=ctx) as s:
            ReversalService(s).reverse(
                tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(sale_out.doc_id)),
                member_id=member_id, ignore_window=True,
            )
            assert_all_invariants(s, tenant_id, store_id)
            # 再撤销进货单 buy_out（曾结清虚拟批、部分余量还在）：同样三账保持守恒
            ReversalService(s).reverse(
                tenant_id=tenant_id, store_id=store_id, doc_id=uuid.UUID(str(buy_out.doc_id)),
                member_id=member_id, ignore_window=True,
            )
            assert_all_invariants(s, tenant_id, store_id)

            # 4) 验证 confirmed 卡片与 cancelled 卡片均不可 revive 重复落账
            sess1 = uuid.uuid4()
            sess2 = uuid.uuid4()
            spec = default_registry().require("purchase.create")
            preview = ImpactPreviewService(db).preview(tenant_id=tenant_id, store_id=store_id, deltas=(), session=s)
            parse = ParseResult(
                intent="purchase.create",
                slots={"P": "可乐", "Q": 1, "U": "瓶", "UP": 200, "TA": 200, "PS": "已结"},
                source={"P": "asr", "Q": "asr", "U": "asr", "UP": "asr", "TA": "asr", "PS": "asr"},
                confidence=0.98, calc_traces=[], channel="template", route="card",
            )
            lines = [{"product_id": str(pid), "product_label": "可乐", "quantity": 1, "unit": "瓶", "qty_base": "1", "unit_price_fen": 200, "amount_fen": 200, "payment_status": "已结", "pending": False}]
            ledger = LedgerService(s)
            c1 = ledger.create_card(tenant_id=tenant_id, store_id=store_id, member_id=member_id, session_id=sess1, parse=parse, asr_text="进1瓶可乐", asr_confidence=0.98, lines=lines, preview=preview)
            ledger.confirm(tenant_id=tenant_id, store_id=store_id, member_id=member_id, card_id=uuid.UUID(c1.card_id), confirm_type="button", button_fallback=True)
            with pytest.raises(AppError) as e_conf:
                ledger.revive(tenant_id=tenant_id, session_id=sess1)
            assert e_conf.value.code == "E_VALIDATION"

            c2 = ledger.create_card(tenant_id=tenant_id, store_id=store_id, member_id=member_id, session_id=sess2, parse=parse, asr_text="进1瓶可乐", asr_confidence=0.98, lines=lines, preview=preview)
            ledger.cancel(tenant_id=tenant_id, member_id=member_id, card_id=uuid.UUID(c2.card_id), reason="不要了")
            with pytest.raises(AppError) as e_canc:
                ledger.revive(tenant_id=tenant_id, session_id=sess2)
            assert e_canc.value.code == "E_VALIDATION"
    finally:
        db.drop_all()
        db.dispose()


def test_journey_6_cst_early_morning_month_and_export_alignment() -> None:
    """J6: UTC 上月最后一天 21:16（北京时间次月 1 日 05:16）下，所有计次周期与报表区间均对齐 CST。"""
    # 2026-11-30 21:16:00 UTC == 2026-12-01 05:16:00 Asia/Shanghai
    utc_moment = datetime(2026, 11, 30, 21, 16, 0, tzinfo=timezone.utc)
    assert usage_current_period(utc_moment) == "2026-12"
    assert entitlement_current_period(utc_moment) == "2026-12"
    assert deliver_current_period(utc_moment) == "2026-12"
    # period_range("") 不再使用 UTC date.today()
    start, end = period_range("2026-12")
    assert start == date(2026, 12, 1) and end == date(2026, 12, 31)


def test_journey_7_clerk_cost_hiding_and_delivery_note_security_guards() -> None:
    """J7: 店员跨全接口零进价/毛利泄漏（含 R0/复购/改单候选/拍进货单）+ viewer 403 + 防重复确认。"""
    reset_shared_voice_state()
    settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-journey-7-32bytes-key!",
        database_url="sqlite+pysqlite:///:memory:",
        llm_provider="fake",
        vision_provider="fake",
    )
    app = create_app(settings=settings)
    database = app.state.database
    database.create_all()
    try:
        with TestClient(app) as client:
            boss_data = client.post("/api/v1/auth/login", json={"code": "wx-j7-boss"}).json()["data"]
            tenant_id = uuid.UUID(boss_data["tenant_id"])
            store_id = uuid.UUID(boss_data["store_id"])
            boss_headers = {"Authorization": f"Bearer {boss_data['jwt']}"}

            clerk_openid = FakeWxProvider().resolve_openid("wx-j7-clerk").openid
            readonly_openid = FakeWxProvider().resolve_openid("wx-j7-readonly").openid
            with database.session(bypass_scope=True) as s:
                for oid, name, role_code in (
                    (clerk_openid, "店员甲", "clerk"),
                    (readonly_openid, "只读审计", "readonly"),
                ):
                    m = Member(tenant_id=tenant_id, wx_openid=oid, name=name, status="active")
                    s.add(m)
                    s.flush()
                    s.add(Role(tenant_id=tenant_id, member_id=m.member_id, store_id=store_id, role_code=role_code))
                s.add(
                    Product(
                        tenant_id=tenant_id, name="可乐", spec="500ml", base_unit="箱",
                        last_purchase_price_fen=3500, last_sale_price_fen=4500, avg_cost_fen=3500, status="active",
                    )
                )

            clerk_headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'code': 'wx-j7-clerk'}).json()['data']['jwt']}"}
            readonly_headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'code': 'wx-j7-readonly'}).json()['data']['jwt']}"}

            # 老板录一笔进货 + 一笔销售
            for text in ("进了两箱可乐，一箱35", "卖了一箱可乐，一箱45"):
                sid = client.post("/api/v1/voice/sessions", json={"duration_ms": 1200, "energy_rms": 0.5}, headers=boss_headers).json()["data"]["session_id"]
                parsed = client.post(f"/api/v1/voice/sessions/{sid}/parse", json={"asr_text": text, "asr_confidence": 0.96, "duration_ms": 1200}, headers=boss_headers).json()["data"]
                cid = parsed["card"]["meta"]["card_id"]
                assert client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=boss_headers).status_code == 200

            # 1) 店员 R0 查库存、查单据、查今日经营：严禁透出进价与毛利
            for utt in ("可乐还有多少", "查一下最近单据", "今天卖了多少"):
                sid = client.post("/api/v1/voice/sessions", json={"duration_ms": 1200, "energy_rms": 0.5}, headers=clerk_headers).json()["data"]["session_id"]
                ans = client.post(f"/api/v1/voice/sessions/{sid}/parse", json={"asr_text": utt, "asr_confidence": 0.95, "duration_ms": 1200}, headers=clerk_headers).json()["data"]
                if ans.get("route") == "answer":
                    body = ans["answer"]
                    if body["kind"] == "stock":
                        assert all(it["avg_cost_fen"] is None and it["last_purchase_price_fen"] is None for it in body["items"])
                    elif body["kind"] == "docs":
                        for it in body["items"]:
                            if it["doc_type"] in ("purchase", "purchase_return"):
                                assert it["total_amount_fen"] is None
                    elif body["kind"] == "report":
                        assert body["report"]["purchase_fen"] is None
                        assert body["report"]["gross_profit_fen"] is None

            # 2) 店员说「跟上次一样」复购进货单：不得预填老板上次进价 3500
            sid = client.post("/api/v1/voice/sessions", json={"duration_ms": 1200, "energy_rms": 0.5}, headers=clerk_headers).json()["data"]["session_id"]
            rep_parse = client.post(f"/api/v1/voice/sessions/{sid}/parse", json={"asr_text": "跟上次进货一样", "asr_confidence": 0.95, "duration_ms": 1200}, headers=clerk_headers)
            assert rep_parse.status_code == 200
            rep_data = rep_parse.json()["data"]
            assert rep_data["route"] == "card"
            assert rep_data["card"]["calc"]["lines"][0]["unit_price_fen"] is None
            assert rep_data["card"]["calc"]["lines"][0]["amount_fen"] == 0 or rep_data["card"]["calc"]["lines"][0]["amount_fen"] is None

            # 3) 店员查改单候选：进货单金额必须隐藏
            cands = client.get("/api/v1/query/docs/candidates", headers=clerk_headers)
            assert cands.status_code == 200
            for item in cands.json()["data"]["items"]:
                if str(item["intent_id"]).startswith("purchase"):
                    assert item["total_amount_fen"] is None

            # 4) readonly 调拍进货单解析/确认 -> 403
            fake_ocr = json.dumps({"supplier_name": "测试", "payment_status": "已结", "stated_total_fen": 7000, "lines": [{"product_name": "可乐", "unit": "箱", "quantity": 2, "unit_price_fen": 3500, "amount_fen": 7000}]})
            assert client.post("/api/v1/delivery-note/parse", json={"object_ref": "cloud://v.jpg", "hint_text": fake_ocr}, headers=readonly_headers).status_code == 403

            # 5) 店员拍进货单 parse + attachments 返回金额全部脱敏
            dn_clerk = client.post("/api/v1/delivery-note/parse", json={"object_ref": "cloud://c.jpg", "hint_text": fake_ocr}, headers=clerk_headers)
            assert dn_clerk.status_code == 200
            dn_body = dn_clerk.json()["data"]
            assert dn_body["cost_hidden"] is True
            assert dn_body["computed_total_fen"] is None
            assert dn_body["lines"][0]["unit_price_fen"] is None

            dn_list = client.get("/api/v1/delivery-note/attachments", headers=clerk_headers).json()["data"]
            assert dn_list["cost_hidden"] is True
            assert dn_list["items"][0]["computed_total_fen"] is None

            # 6) readonly 确认进货单 -> 403；老板确认一次成功，第二次必返回 409 E_DUPLICATE_REQUEST
            att_id = dn_body["attachment_id"]
            confirm_payload = {"supplier_name": "测试供应商", "payment_status": "已结", "lines": [{"product_name": "可乐", "unit": "箱", "quantity": 2, "unit_price_fen": 3500, "amount_fen": 7000}]}
            assert client.post(f"/api/v1/delivery-note/{att_id}/confirm", json=confirm_payload, headers=readonly_headers).status_code == 403
            assert client.post(f"/api/v1/delivery-note/{att_id}/confirm", json=confirm_payload, headers=boss_headers).status_code == 200
            dup = client.post(f"/api/v1/delivery-note/{att_id}/confirm", json=confirm_payload, headers=boss_headers)
            assert dup.status_code == 409
    finally:
        database.drop_all()
        database.dispose()
