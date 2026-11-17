"""QA Round-2 追问探针 5（临时，不提交）：撤销边界 + 重复确认 + 实体写入可达性。

运行： cd server && python -m pytest tests/_qa_r5.py -q -s
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.common.errors import AppError
from app.domain.inventory_service import InventoryService
from app.models.catalog import Product
from app.models.doc import DocHeader
from app.services.ledger import LedgerService
from t04_fixtures import Seed, count_rows, create_card_for, lines_for, seed_shop

SEED_QTY = Decimal("192")


@pytest.fixture()
def shop() -> Seed:
    seeded = seed_shop()
    try:
        yield seeded
    finally:
        seeded.database.drop_all()
        seeded.database.dispose()


def _confirm(
    seeded: Seed,
    *,
    intent_id: str = "purchase.create",
    slots: dict | None = None,
    source: dict | None = None,
    lines: list | None = None,
    confirm_type: str = "button",
    spoken_text: str | None = None,
    with_product: bool = True,
):  # noqa: ANN201
    with seeded.database.session(context=seeded.context) as session:
        ledger, creation = create_card_for(
            seeded,
            session=session,
            intent_id=intent_id,
            slots=slots,
            source=source,
            lines=lines if lines is not None else lines_for(seeded, **({} if with_product else {"with_product": False})),
        )
        outcome = ledger.confirm(
            tenant_id=seeded.tenant_id,
            store_id=seeded.store_id,
            member_id=seeded.member_id,
            card_id=uuid.UUID(creation.card_id),
            confirm_type=confirm_type,
            spoken_text=spoken_text,
            button_fallback=confirm_type == "button",
        )
    return creation, outcome


def _qty(seeded: Seed) -> Decimal:
    with seeded.database.session(context=seeded.context) as session:
        row = InventoryService(session).get_qty_base(
            tenant_id=seeded.tenant_id, store_id=seeded.store_id, product_id=seeded.product_id
        )
        return Decimal(str(row.qty_base)) if row is not None else Decimal("0")


# ① 重复确认（CAS 抢占）——服务层，绕开 HTTP 的 pending 误伤
def test_double_confirm_service(shop: Seed) -> None:
    creation, first = _confirm(shop)
    print("\nfirst:", first.confirmed, first.doc_no, "docs:", count_rows(shop.database, DocHeader))
    with shop.database.session(context=shop.context) as session:
        ledger = LedgerService(session)
        try:
            second = ledger.confirm(
                tenant_id=shop.tenant_id,
                store_id=shop.store_id,
                member_id=shop.member_id,
                card_id=uuid.UUID(creation.card_id),
                confirm_type="button",
                spoken_text=None,
                button_fallback=True,
            )
            print("second:", second.confirmed, second.doc_no, "detail:", second.detail[:60])
            print("status:", second.card_status)
        except AppError as exc:
            print("second raised:", exc.error_code, exc.message[:90])
    print("docs after:", count_rows(shop.database, DocHeader), "qty:", _qty(shop))


# ② 没有任何历史单就撤销
def test_undo_without_any_doc(shop: Seed) -> None:
    try:
        creation, out = _confirm(shop, intent_id="sys.undo", confirm_type="voice2", spoken_text="确认")
        print("\nundo(no doc) ->", out.confirmed, out.doc_no, out.detail[:80])
    except AppError as exc:
        print("\nundo(no doc) raised:", exc.error_code, "|", exc.message[:80], "| data:", dict(exc.data or {}))
    print("docs:", count_rows(shop.database, DocHeader), "qty:", _qty(shop))


# ③ 撤销两次
def test_undo_twice(shop: Seed) -> None:
    _, purchase = _confirm(shop)
    _, undo1 = _confirm(shop, intent_id="sys.undo", confirm_type="voice2", spoken_text="确认")
    print("\npurchase:", purchase.doc_no, "undo1:", undo1.doc_no, "qty:", _qty(shop), "docs:", count_rows(shop.database, DocHeader))
    try:
        _, undo2 = _confirm(shop, intent_id="sys.undo", confirm_type="voice2", spoken_text="确认")
        print("undo2 ->", undo2.confirmed, undo2.doc_no, "reversal:", undo2.reversal_doc_no, "detail:", undo2.detail[:100])
    except AppError as exc:
        print("undo2 raised:", exc.error_code, "|", exc.message[:90])
    with shop.database.session(context=shop.context) as session:
        rows = session.execute(select(DocHeader.doc_no, DocHeader.intent_id, DocHeader.status, DocHeader.total_amount_fen)).all()
        print("单据:", [(r.doc_no, r.intent_id, r.status, str(r.total_amount_fen)) for r in rows])
    print("qty:", _qty(shop), "docs:", count_rows(shop.database, DocHeader))


# ④ 「上一单」跨租户
def test_resolve_target_cross_tenant(shop: Seed) -> None:
    _, purchase = _confirm(shop)
    with shop.database.session(context=shop.context) as session:
        ledger = LedgerService(session)
        mine = ledger._resolve_target_doc(tenant_id=shop.tenant_id, raw_ref="上一单")  # noqa: SLF001
        foreign = ledger._resolve_target_doc(tenant_id=uuid.uuid4(), raw_ref="上一单")  # noqa: SLF001
        by_no_mine = ledger._resolve_target_doc(tenant_id=shop.tenant_id, raw_ref=str(purchase.doc_no))  # noqa: SLF001
        by_no_foreign = ledger._resolve_target_doc(tenant_id=uuid.uuid4(), raw_ref=str(purchase.doc_no))  # noqa: SLF001
    print("\n上一单(本租户):", mine.doc_no if mine else None)
    print("上一单(别的租户):", foreign.doc_no if foreign else None)
    print("单号(本租户):", by_no_mine.doc_no if by_no_mine else None)
    print("单号(别的租户):", by_no_foreign.doc_no if by_no_foreign else None)


# ⑤ 实体写入到底可不可达：给空明细能不能建立商品
def test_entity_writer_reachable_with_empty_lines(shop: Seed) -> None:
    try:
        _, out = _confirm(shop, intent_id="product.create", slots={"P": "雪碧"}, source={"P": "asr"}, lines=[])
        print("\nproduct.create(空明细) ->", out.confirmed, "doc:", out.doc_no, "entity:", getattr(out, "entity_id", None))
    except AppError as exc:
        print("\nproduct.create(空明细) raised:", exc.error_code, "|", exc.message[:110], "| data:", dict(exc.data or {}))
    with shop.database.session(context=shop.context) as session:
        names = [p.name for p in session.execute(select(Product)).scalars().all()]
    print("products:", names)


# ⑥ 撤销后今日报表口径（反做单会不会被当成又一笔采购）
def test_report_after_undo(shop: Seed) -> None:
    from datetime import date

    from app.domain.report_service import ReportService

    _, purchase = _confirm(shop)
    with shop.database.session(context=shop.context) as session:
        r1 = ReportService(session).today(tenant_id=shop.tenant_id, store_id=shop.store_id, biz_date=date.today())
        print("\n进货后 today: purchase_fen=", r1.purchase_fen, "sales_fen=", r1.sales_fen, "doc_count=", r1.doc_count)
    _, undo = _confirm(shop, intent_id="sys.undo", confirm_type="voice2", spoken_text="确认")
    with shop.database.session(context=shop.context) as session:
        r2 = ReportService(session).today(tenant_id=shop.tenant_id, store_id=shop.store_id, biz_date=date.today())
        print("撤销后 today: purchase_fen=", r2.purchase_fen, "sales_fen=", r2.sales_fen, "doc_count=", r2.doc_count)
        print("purchase.doc_no=", purchase.doc_no, "undo.doc_no=", undo.doc_no, "reversal=", undo.reversal_doc_no)
