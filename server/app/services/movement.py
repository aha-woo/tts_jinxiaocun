"""库存流水：单据、反做共用的唯一写入点。"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.models.doc import DocLine
from app.models.inventory import InventoryMovement


def reason_of(intent_id: str) -> str | None:
    """意图 → 流水原因；非库存变动返回 None。"""
    if intent_id.startswith("purchase_return."):
        return "purchase_return"
    if intent_id.startswith("purchase."):
        return "purchase"
    if intent_id.startswith("sales_return."):
        return "sales_return"
    if intent_id.startswith("sales."):
        return "sale"
    if intent_id == "stock.loss":
        return "loss"
    if intent_id.startswith("stock.take"):
        return "count_adjust"
    return None


def _base_unit_cost(line: DocLine) -> int | None:
    qty_base = Decimal(line.qty_base)
    if line.cost_snapshot_fen is not None and qty_base > 0:
        return int(
            (Decimal(line.cost_snapshot_fen) / qty_base).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    if line.amount_fen is not None and qty_base > 0:
        return int(
            (Decimal(line.amount_fen) / qty_base).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    if line.unit_price_fen is not None and Decimal(line.qty) == qty_base:
        return int(line.unit_price_fen)
    return None


def record_doc_movements(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    doc_id: uuid.UUID,
    member_id: uuid.UUID,
    reason: str,
    sign: int = 1,
    partner_id: uuid.UUID | None = None,
) -> None:
    """按单据行写流水（严格使用基本单位成本与正确符号）。``sign=1`` 正向，``sign=-1`` 反做。"""
    rows = list(
        session.execute(  # type: ignore[attr-defined]
            select(DocLine).where(DocLine.tenant_id == tenant_id, DocLine.doc_id == doc_id)
        ).scalars()
    )
    for line in rows:
        qty_base = Decimal(line.qty_base)
        if qty_base == 0:
            continue
        session.add(  # type: ignore[attr-defined]
            InventoryMovement(
                tenant_id=tenant_id,
                store_id=store_id,
                product_id=line.product_id,
                batch_id=line.batch_id,
                doc_id=doc_id,
                doc_line_id=line.line_id,
                reason=reason,
                qty_base=qty_base * sign,
                unit_cost_fen=_base_unit_cost(line),
                cost_status=str(line.cost_status or "final"),
                partner_id=partner_id,
                created_by=member_id,
            )
        )
    session.flush()  # type: ignore[attr-defined]
