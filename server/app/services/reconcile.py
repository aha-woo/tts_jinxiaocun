"""三账夜间核对服务（只读检查，绝不自动改账）。

每日对账检查三项一致性：
1. ``Inventory.qty_base == SUM(InventoryMovement.qty_base)``（余额与流水核对）；
2. 开启 ``track_batch=True`` 的商品：``SUM(StockBatch.qty_left) == Inventory.qty_base``（批次余量核对）；
3. 统计未结清的暂估/未知成本销售行（``cost_status IN ('provisional','unknown')``）及当前负库存商品数。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.doc import DocHeader, DocLine
from app.models.inventory import Inventory, InventoryMovement, StockBatch
from app.services import batch as batch_service

__all__ = ["reconcile_store"]


def reconcile_store(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
) -> dict[str, Any]:
    """核对门店三账一致性并返回核对摘要。"""
    inv_rows = list(
        session.execute(
            select(Inventory, Product)
            .join(
                Product,
                (Product.tenant_id == Inventory.tenant_id)
                & (Product.product_id == Inventory.product_id),
            )
            .where(Inventory.tenant_id == tenant_id, Inventory.store_id == store_id)
        ).all()
    )
    mov_sums = {
        pid: Decimal(str(total or 0))
        for pid, total in session.execute(
            select(InventoryMovement.product_id, func.coalesce(func.sum(InventoryMovement.qty_base), 0))
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.store_id == store_id,
            )
            .group_by(InventoryMovement.product_id)
        ).all()
    }
    batch_sums = {
        pid: Decimal(str(total or 0))
        for pid, total in session.execute(
            select(StockBatch.product_id, func.coalesce(func.sum(StockBatch.qty_left), 0))
            .where(
                StockBatch.tenant_id == tenant_id,
                StockBatch.store_id == store_id,
                StockBatch.status != "exhausted",
            )
            .group_by(StockBatch.product_id)
        ).all()
    }
    policy = batch_service.policy_for(session, tenant_id)

    movement_mismatches: list[dict[str, Any]] = []
    batch_mismatches: list[dict[str, Any]] = []
    negative_products: list[dict[str, Any]] = []

    for inv, prod in inv_rows:
        inv_qty = Decimal(inv.qty_base)
        mov_qty = mov_sums.get(inv.product_id, Decimal("0"))
        if abs(inv_qty - mov_qty) > Decimal("0.0001"):
            movement_mismatches.append(
                {
                    "product_id": str(inv.product_id),
                    "product_name": prod.name,
                    "inventory_qty": str(inv_qty),
                    "movement_sum_qty": str(mov_qty),
                }
            )
        if batch_service.product_tracks_batch(session, tenant_id, inv.product_id, policy):
            b_qty = batch_sums.get(inv.product_id, Decimal("0"))
            if abs(inv_qty - b_qty) > Decimal("0.0001"):
                batch_mismatches.append(
                    {
                        "product_id": str(inv.product_id),
                        "product_name": prod.name,
                        "inventory_qty": str(inv_qty),
                        "batch_sum_qty": str(b_qty),
                    }
                )
        if inv_qty < 0:
            negative_products.append(
                {
                    "product_id": str(inv.product_id),
                    "product_name": prod.name,
                    "qty_base": str(inv_qty),
                    "unit": prod.base_unit,
                }
            )

    provisional_lines = int(
        session.execute(
            select(func.count())
            .select_from(DocLine)
            .join(
                DocHeader,
                (DocHeader.tenant_id == DocLine.tenant_id) & (DocHeader.doc_id == DocLine.doc_id),
            )
            .where(
                DocLine.tenant_id == tenant_id,
                DocLine.store_id == store_id,
                DocHeader.status == "confirmed",
                DocLine.cost_status.in_(("provisional", "unknown")),
            )
        ).scalar_one()
        or 0
    )

    ok_flag = len(movement_mismatches) == 0 and len(batch_mismatches) == 0
    return {
        "ok": ok_flag,
        "checked_products": len(inv_rows),
        "movement_mismatches": movement_mismatches,
        "batch_mismatches": batch_mismatches,
        "negative_count": len(negative_products),
        "negative_products": negative_products[:20],
        "provisional_or_unknown_cost_lines": provisional_lines,
    }
