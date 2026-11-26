"""欠款页直接核销与客户/供应商对账单服务。

设计约束：
- 收付款落账必须经 ``LedgerService.create_card`` + ``LedgerService.confirm`` 统一执行，不绕开账务服务直接改账；
- 原单标记 ``source='manual'`` 并留痕。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.db.session import Database
from app.models.doc import ArAp, DocHeader
from app.models.org import Store
from app.models.partner import Partner
from app.models.voice import VoiceSession
from app.services.ledger import LedgerService
from app.utils.date_utils import today_in_tz
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.output_schema import ParseResult

__all__ = [
    "partner_statement",
    "settle_partner_debt",
]


def settle_partner_debt(
    session: Session,
    database: Database,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    partner_id: uuid.UUID,
    direction: str,
    amount_fen: int,
    method: str = "微信",
) -> dict[str, Any]:
    """在欠款页登记一笔收款/付款核销（统一经 LedgerService 落账）。"""
    if direction not in ("receivable", "payable"):
        raise AppError("E_VALIDATION", message="direction 必须是 receivable 或 payable")
    if int(amount_fen) <= 0:
        raise AppError("E_VALIDATION", message="核销金额必须大于 0")

    partner = session.execute(
        select(Partner)
        .where(Partner.tenant_id == tenant_id, Partner.partner_id == partner_id)
        .limit(1)
    ).scalar_one_or_none()
    if partner is None:
        raise AppError("E_NOT_FOUND", message="往来单位不存在")

    open_rows = list(
        session.execute(
            select(ArAp).where(
                ArAp.tenant_id == tenant_id,
                ArAp.partner_id == partner_id,
                ArAp.direction == direction,
                ArAp.status.in_(("open", "partial")),
            )
        ).scalars()
    )
    total_open = sum(max(0, int(r.amount_fen) - int(r.settled_amount_fen)) for r in open_rows)
    if total_open <= 0:
        raise AppError("E_VALIDATION", message=f"「{partner.name}」当前没有未结欠款可核销")
    if int(amount_fen) > total_open:
        raise AppError(
            "E_VALIDATION",
            message=f"核销金额 ¥{amount_fen / 100:.2f} 超过了当前未结欠款 ¥{total_open / 100:.2f}",
        )

    is_receive = direction == "receivable"
    intent_id = "payment.receive" if is_receive else "payment.pay"
    ar_delta = "receivable_decrease" if is_receive else "payable_decrease"
    today_iso = today_in_tz("Asia/Shanghai")
    pm = (method or "微信").strip() or "微信"

    slots: dict[str, Any] = {
        "C" if is_receive else "S": partner.name,
        "AMT": int(amount_fen),
        "PM": pm,
        "D": today_iso,
        "PS": "已结",
    }
    source = {k: "asr" for k in slots}
    parse = ParseResult(
        intent=intent_id,
        slots=slots,
        source=source,  # type: ignore[arg-type]
        confidence=0.99,
        calc_traces=[f"{'收款' if is_receive else '付款'}核销 ¥{amount_fen / 100:.2f}（{pm}）"],
        channel="template",
        route="card",
    )

    preview_lines = [
        {
            "product_id": None,
            "product_label": partner.name,
            "partner_id": str(partner.partner_id),
            "quantity": 1,
            "unit": "笔",
            "qty_base": "1",
            "amount_fen": int(amount_fen),
            "payment_status": "已结",
            "calc_trace": parse.calc_traces[0],
            "pending": False,
        }
    ]
    deltas = build_deltas(
        intent_id=intent_id,
        side_effect={"inventory_delta": "none", "ar_ap_delta": ar_delta},
        lines=preview_lines,
    )
    preview = ImpactPreviewService(database).preview(
        tenant_id=tenant_id,
        store_id=store_id,
        deltas=deltas,
        session=None if database.is_pg else session,
    )

    vs = VoiceSession(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        state="CARD_SHOWN",
        clarify_round=0,
        expires_at=datetime.now(timezone.utc),
    )
    session.add(vs)
    session.flush()

    ledger = LedgerService(session)
    card_res = ledger.create_card(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        session_id=vs.session_id,
        parse=parse,
        asr_text=f"欠款核销：{partner.name} ¥{amount_fen / 100:.2f}（{pm}）",
        asr_confidence=1.0,
        lines=[],
        preview=preview,
    )
    confirmed = ledger.confirm(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        card_id=uuid.UUID(card_res.card_id),
        confirm_type="button",
        button_fallback=True,
    )
    if not confirmed.confirmed or not confirmed.doc_id:
        raise AppError(
            confirmed.error_code or "E_VALIDATION",
            message=confirmed.detail or "欠款核销入账失败",
        )

    doc_uuid = uuid.UUID(confirmed.doc_id)
    doc = session.execute(
        select(DocHeader).where(DocHeader.tenant_id == tenant_id, DocHeader.doc_id == doc_uuid).limit(1)
    ).scalar_one_or_none()
    if doc is not None:
        doc.source = "manual"
        if int(doc.total_amount_fen or 0) <= 0:
            doc.total_amount_fen = int(amount_fen)
    session.flush()

    remaining_fen = total_open - int(amount_fen)
    return {
        "doc_id": confirmed.doc_id,
        "doc_no": confirmed.doc_no,
        "partner_id": str(partner.partner_id),
        "partner_name": partner.name,
        "direction": direction,
        "settled_fen": int(amount_fen),
        "remaining_outstanding_fen": max(0, remaining_fen),
        "method": pm,
    }


def partner_statement(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    partner_id: uuid.UUID,
    direction: str | None = None,
) -> dict[str, Any]:
    """生成客户/供应商对账单数据（供端侧绘制成对账单图片发微信）。"""
    partner = session.execute(
        select(Partner)
        .where(Partner.tenant_id == tenant_id, Partner.partner_id == partner_id)
        .limit(1)
    ).scalar_one_or_none()
    if partner is None:
        raise AppError("E_NOT_FOUND", message="往来单位不存在")

    store = session.execute(
        select(Store).where(Store.tenant_id == tenant_id, Store.store_id == store_id).limit(1)
    ).scalar_one_or_none()
    store_name = str(store.name) if store is not None else "本店"

    stmt = select(ArAp).where(
        ArAp.tenant_id == tenant_id,
        ArAp.partner_id == partner_id,
    )
    if direction in ("receivable", "payable"):
        stmt = stmt.where(ArAp.direction == direction)
    stmt = stmt.order_by(ArAp.created_at.desc())
    ar_rows = list(session.execute(stmt).scalars())

    doc_ids = [r.doc_id for r in ar_rows]
    docs_by_id: dict[uuid.UUID, DocHeader] = {}
    if doc_ids:
        for d in session.execute(
            select(DocHeader).where(DocHeader.tenant_id == tenant_id, DocHeader.doc_id.in_(doc_ids))
        ).scalars():
            docs_by_id[d.doc_id] = d

    items: list[dict[str, Any]] = []
    total_outstanding = 0
    for r in ar_rows:
        outstanding = max(0, int(r.amount_fen) - int(r.settled_amount_fen))
        if r.status == "settled" and outstanding == 0:
            continue
        doc = docs_by_id.get(r.doc_id)
        total_outstanding += outstanding
        items.append(
            {
                "ar_ap_id": str(r.ar_ap_id),
                "doc_id": str(r.doc_id),
                "doc_no": doc.doc_no if doc is not None else str(r.doc_id)[:8],
                "biz_date": doc.biz_date.isoformat() if doc is not None else today_in_tz("Asia/Shanghai"),
                "direction": r.direction,
                "amount_fen": int(r.amount_fen),
                "settled_amount_fen": int(r.settled_amount_fen),
                "outstanding_fen": outstanding,
                "status": r.status,
            }
        )

    return {
        "store_name": store_name,
        "partner_id": str(partner.partner_id),
        "partner_name": partner.name,
        "direction": direction or ("receivable" if partner.type == "customer" else "payable"),
        "generated_at": today_in_tz("Asia/Shanghai"),
        "total_outstanding_fen": total_outstanding,
        "items": items,
    }
