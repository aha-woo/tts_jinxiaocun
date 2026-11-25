"""单位换算教学与原子基本单位升级（懒建档 M1-3）。

场景
----
- 商户最初进货「进了 10 箱可乐」，当时还没教瓶数，系统建档 ``base_unit="箱"``；
- 后来卖货「卖了 5 瓶可乐」，补充教学「1 箱 = 24 瓶」；
- 如果把新教学仅存成 ``from=箱, to=瓶`` 且不升级基本单位，或者反过来仅改 ``product.base_unit="瓶"``
  却不倍乘旧库存/流水/批次，历史账目就会 10 箱直接变成 10 瓶。
- 本服务把「写入审核换算系数 + 原基本单位全部同事务倍乘到细粒度单位」做成单一原子事务。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.catalog import Product, UnitConversion
from app.models.doc import DocLine
from app.models.inventory import Inventory, InventoryMovement, StockBatch
from app.services.audit import AuditService
from app.services.gaps import resolve_gaps

__all__ = [
    "UnitRatioOutcome",
    "set_unit_ratio_and_maybe_upgrade",
]


@dataclass(frozen=True)
class UnitRatioOutcome:
    """设置单位换算后的结果。"""

    product_id: str
    base_unit: str
    from_unit: str
    to_unit: str
    ratio: str
    upgraded_base_unit: bool
    updated_inventory_rows: int
    updated_movement_rows: int
    updated_batch_rows: int
    updated_doc_lines: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "base_unit": self.base_unit,
            "from_unit": self.from_unit,
            "to_unit": self.to_unit,
            "ratio": self.ratio,
            "upgraded_base_unit": self.upgraded_base_unit,
            "updated_inventory_rows": self.updated_inventory_rows,
            "updated_movement_rows": self.updated_movement_rows,
            "updated_batch_rows": self.updated_batch_rows,
            "updated_doc_lines": self.updated_doc_lines,
        }


def _scale_fen_down(value: int | None, ratio: Decimal) -> int | None:
    if value is None:
        return None
    scaled = (Decimal(int(value)) / ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(scaled)


def set_unit_ratio_and_maybe_upgrade(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    product_id: uuid.UUID,
    from_unit: str,
    to_unit: str,
    ratio: Decimal,
) -> UnitRatioOutcome:
    """设置 ``1 from_unit = ratio × to_unit``；若把原基本单位拆成更细单位则原子升级全部账目。"""
    frm = (from_unit or "").strip()
    to = (to_unit or "").strip()
    if not frm or not to or frm == to:
        raise AppError("E_VALIDATION", message="原单位与目标单位必须存在且不能相同")
    if ratio <= 0:
        raise AppError("E_VALIDATION", message="换算系数必须大于 0")

    product = session.execute(
        select(Product).where(Product.tenant_id == tenant_id, Product.product_id == product_id).limit(1)
    ).scalar_one_or_none()
    if product is None:
        raise AppError("E_NOT_FOUND", message="商品不存在")

    old_base = str(product.base_unit)
    upgraded = False
    inv_count = 0
    move_count = 0
    batch_count = 0
    doc_line_count = 0

    if to == old_base:
        conv_from, conv_to, conv_ratio = frm, to, ratio
    elif frm == old_base:
        # 原子升级基本单位：原 base_unit (=from_unit) -> 更细粒度 to_unit
        upgraded = True
        conv_from, conv_to, conv_ratio = frm, to, ratio
        product.base_unit = to
        product.avg_cost_fen = _scale_fen_down(product.avg_cost_fen, ratio)
        product.last_purchase_price_fen = _scale_fen_down(product.last_purchase_price_fen, ratio)
        product.last_sale_price_fen = _scale_fen_down(product.last_sale_price_fen, ratio)
        if product.low_stock_qty is not None:
            product.low_stock_qty = (Decimal(str(product.low_stock_qty)) * ratio).quantize(Decimal("0.0001"))

        for inv in session.execute(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id,
                Inventory.product_id == product_id,
            )
        ).scalars():
            inv.qty_base = (Decimal(str(inv.qty_base)) * ratio).quantize(Decimal("0.0001"))
            inv.avg_cost_fen = _scale_fen_down(inv.avg_cost_fen, ratio)
            inv.version = int(inv.version) + 1
            inv_count += 1

        for move in session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.product_id == product_id,
            )
        ).scalars():
            move.qty_base = (Decimal(str(move.qty_base)) * ratio).quantize(Decimal("0.0001"))
            move.unit_cost_fen = _scale_fen_down(move.unit_cost_fen, ratio)
            move_count += 1

        for batch in session.execute(
            select(StockBatch).where(
                StockBatch.tenant_id == tenant_id,
                StockBatch.product_id == product_id,
            )
        ).scalars():
            batch.qty_in = (Decimal(str(batch.qty_in)) * ratio).quantize(Decimal("0.0001"))
            batch.qty_left = (Decimal(str(batch.qty_left)) * ratio).quantize(Decimal("0.0001"))
            batch.unit_cost_fen = _scale_fen_down(batch.unit_cost_fen, ratio)
            batch_count += 1

        for dline in session.execute(
            select(DocLine).where(
                DocLine.tenant_id == tenant_id,
                DocLine.product_id == product_id,
            )
        ).scalars():
            dline.qty_base = (Decimal(str(dline.qty_base)) * ratio).quantize(Decimal("0.0001"))
            doc_line_count += 1
    else:
        raise AppError(
            "E_VALIDATION",
            message=f"请填写与当前基本单位「{old_base}」之间的换算关系",
            data={"base_unit": old_base},
        )

    existing = session.execute(
        select(UnitConversion)
        .where(
            UnitConversion.tenant_id == tenant_id,
            UnitConversion.product_id == product_id,
            UnitConversion.from_unit == conv_from,
            UnitConversion.to_unit == conv_to,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            UnitConversion(
                tenant_id=tenant_id,
                product_id=product_id,
                from_unit=conv_from,
                to_unit=conv_to,
                ratio=conv_ratio,
                status="approved",
            )
        )
    else:
        existing.ratio = conv_ratio
        existing.status = "approved"

    resolve_gaps(
        session,
        tenant_id=tenant_id,
        store_id=store_id,
        kind="unit_ratio",
        ref_entity="product",
        ref_id=product_id,
    )
    session.flush()

    AuditService(session).record(
        tenant_id=tenant_id,
        action="product.unit_upgrade",
        entity="product",
        entity_id=str(product_id),
        member_id=member_id,
        detail={
            "from_unit": conv_from,
            "to_unit": conv_to,
            "ratio": str(conv_ratio),
            "upgraded_base_unit": upgraded,
            "old_base_unit": old_base,
            "new_base_unit": str(product.base_unit),
            "updated_rows": {
                "inventory": inv_count,
                "movement": move_count,
                "batch": batch_count,
                "doc_line": doc_line_count,
            },
        },
    )

    return UnitRatioOutcome(
        product_id=str(product_id),
        base_unit=str(product.base_unit),
        from_unit=conv_from,
        to_unit=conv_to,
        ratio=str(conv_ratio),
        upgraded_base_unit=upgraded,
        updated_inventory_rows=inv_count,
        updated_movement_rows=move_count,
        updated_batch_rows=batch_count,
        updated_doc_lines=doc_line_count,
    )
