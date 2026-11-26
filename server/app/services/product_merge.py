"""事后合并重复商品（M2/V1 补齐：合并库存、流水、单据行、批次、盘点行与别名）。

原则：
1. 在同一个事务里将源商品的所有历史记录合并到目标商品，若单位不同则按已登记换算或标准度量衡折算；
2. 源商品保留作 ``status='merged', merged_into=target_id``，绝不物理删行；
3. 自动把源商品名称及既有别名保留在目标商品的别名库，后续语音再叫旧名字直接命中目标商品；
4. 记录 ``product.merge`` 审计日志并关闭相关的重复提醒待处理条目。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.doc import DocLine
from app.models.inventory import CountLine, Inventory, InventoryMovement, StockBatch
from app.services.audit import AuditService
from app.services.gaps import resolve_gaps
from app.voice.unit_converter import standard_weight_ratio

__all__ = [
    "MergeOutcome",
    "merge_products",
]


@dataclass(frozen=True)
class MergeOutcome:
    """合并结果统计。"""

    source_product_id: str
    target_product_id: str
    target_name: str
    target_base_unit: str
    unit_factor: str
    moved_inventory_rows: int
    moved_doc_lines: int
    moved_movements: int
    moved_batches: int
    moved_aliases: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_product_id": self.source_product_id,
            "target_product_id": self.target_product_id,
            "target_name": self.target_name,
            "target_base_unit": self.target_base_unit,
            "unit_factor": self.unit_factor,
            "moved_inventory_rows": self.moved_inventory_rows,
            "moved_doc_lines": self.moved_doc_lines,
            "moved_movements": self.moved_movements,
            "moved_batches": self.moved_batches,
            "moved_aliases": self.moved_aliases,
        }


def _resolve_unit_factor(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    source: Product,
    target: Product,
) -> Decimal:
    src_u = str(source.base_unit).strip()
    dst_u = str(target.base_unit).strip()
    if not src_u or not dst_u or src_u == dst_u:
        return Decimal("1")
    std = standard_weight_ratio(src_u, dst_u)
    if std is not None:
        return std
    for pid, f_u, t_u, invert in (
        (source.product_id, src_u, dst_u, False),
        (target.product_id, src_u, dst_u, False),
        (target.product_id, dst_u, src_u, True),
        (source.product_id, dst_u, src_u, True),
    ):
        conv = session.execute(
            select(UnitConversion)
            .where(
                UnitConversion.tenant_id == tenant_id,
                UnitConversion.product_id == pid,
                UnitConversion.from_unit == f_u,
                UnitConversion.to_unit == t_u,
                UnitConversion.status == "approved",
            )
            .limit(1)
        ).scalar_one_or_none()
        if conv is not None and Decimal(conv.ratio) > 0:
            r = Decimal(str(conv.ratio))
            return (Decimal("1") / r).quantize(Decimal("0.000001")) if invert else r
    raise AppError(
        "E_VALIDATION",
        message=(
            f"「{source.name}」（单位 {src_u}）和「{target.name}」（单位 {dst_u}）基本单位不同，"
            f"请先设好「1 {src_u} = 多少 {dst_u}」再合并"
        ),
    )


def merge_products(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    source_product_id: uuid.UUID,
    target_product_id: uuid.UUID,
) -> MergeOutcome:
    """将重复商品 ``source_product_id`` 合并入 ``target_product_id``。"""
    if source_product_id == target_product_id:
        raise AppError("E_VALIDATION", message="不能把同一件商品合并给自己")

    source = session.execute(
        select(Product).where(
            Product.tenant_id == tenant_id, Product.product_id == source_product_id
        ).limit(1)
    ).scalar_one_or_none()
    target = session.execute(
        select(Product).where(
            Product.tenant_id == tenant_id, Product.product_id == target_product_id
        ).limit(1)
    ).scalar_one_or_none()

    if source is None or target is None:
        raise AppError("E_NOT_FOUND", message="要合并的商品不存在")
    if str(source.status) == "merged":
        raise AppError("E_DUPLICATE_REQUEST", message="这个商品已经合并过了")
    if str(target.status) == "merged":
        raise AppError("E_VALIDATION", message="目标商品已被合并到其他商品，不能作为合并终点")

    factor = _resolve_unit_factor(session, tenant_id=tenant_id, source=source, target=target)

    # 1. 合并库存余额（按 store_id）
    src_invs = list(
        session.execute(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id, Inventory.product_id == source.product_id
            )
        ).scalars()
    )
    moved_inv = len(src_invs)
    for src_inv in src_invs:
        src_qty = (Decimal(src_inv.qty_base) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        src_cost = (
            int((Decimal(src_inv.avg_cost_fen) / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if src_inv.avg_cost_fen is not None and factor != 0
            else None
        )
        dst_inv = session.execute(
            select(Inventory)
            .where(
                Inventory.tenant_id == tenant_id,
                Inventory.store_id == src_inv.store_id,
                Inventory.product_id == target.product_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if dst_inv is None:
            src_inv.product_id = target.product_id
            src_inv.qty_base = src_qty
            src_inv.avg_cost_fen = src_cost
            src_inv.negative_flag = src_qty < 0
            src_inv.version += 1
        else:
            dst_qty = Decimal(dst_inv.qty_base)
            new_qty = (dst_qty + src_qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if dst_inv.avg_cost_fen is not None and src_cost is not None and dst_qty > 0 and src_qty > 0 and new_qty > 0:
                total_cost = dst_qty * Decimal(dst_inv.avg_cost_fen) + src_qty * Decimal(src_cost)
                dst_inv.avg_cost_fen = int((total_cost / new_qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            elif dst_inv.avg_cost_fen is None and src_cost is not None:
                dst_inv.avg_cost_fen = src_cost
            dst_inv.qty_base = new_qty
            dst_inv.negative_flag = new_qty < 0
            dst_inv.version += 1
            session.delete(src_inv)

    # 2. 迁移单据行、流水、批次、盘点行
    doc_lines = list(
        session.execute(
            select(DocLine).where(
                DocLine.tenant_id == tenant_id, DocLine.product_id == source.product_id
            )
        ).scalars()
    )
    for dl in doc_lines:
        dl.product_id = target.product_id
        dl.qty_base = (Decimal(dl.qty_base) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    movements = list(
        session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.product_id == source.product_id,
            )
        ).scalars()
    )
    for mv in movements:
        mv.product_id = target.product_id
        mv.qty_base = (Decimal(mv.qty_base) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if mv.unit_cost_fen is not None and factor != 1:
            mv.unit_cost_fen = int((Decimal(mv.unit_cost_fen) / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    batches = list(
        session.execute(
            select(StockBatch).where(
                StockBatch.tenant_id == tenant_id, StockBatch.product_id == source.product_id
            )
        ).scalars()
    )
    for bt in batches:
        bt.product_id = target.product_id
        bt.qty_in = (Decimal(bt.qty_in) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        bt.qty_left = (Decimal(bt.qty_left) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if bt.unit_cost_fen is not None and factor != 1:
            bt.unit_cost_fen = int((Decimal(bt.unit_cost_fen) / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    count_lines = list(
        session.execute(
            select(CountLine).where(
                CountLine.tenant_id == tenant_id, CountLine.product_id == source.product_id
            )
        ).scalars()
    )
    for cl in count_lines:
        dup_cl = session.execute(
            select(CountLine)
            .where(
                CountLine.tenant_id == tenant_id,
                CountLine.count_id == cl.count_id,
                CountLine.product_id == target.product_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if dup_cl is None:
            cl.product_id = target.product_id
            cl.qty_book = (Decimal(cl.qty_book) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if cl.qty_counted is not None:
                cl.qty_counted = (Decimal(cl.qty_counted) * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            session.delete(cl)

    # 3. 迁移别名，并把源商品名挂成目标商品的别名，防止以后再说旧名建新货
    existing_target_aliases = {
        (a.member_id, str(a.alias_text))
        for a in session.execute(
            select(ProductAlias).where(
                ProductAlias.tenant_id == tenant_id, ProductAlias.product_id == target.product_id
            )
        ).scalars()
    }
    src_aliases = list(
        session.execute(
            select(ProductAlias).where(
                ProductAlias.tenant_id == tenant_id, ProductAlias.product_id == source.product_id
            )
        ).scalars()
    )
    moved_aliases = 0
    for al in src_aliases:
        key = (al.member_id, str(al.alias_text))
        if key in existing_target_aliases:
            session.delete(al)
        else:
            al.product_id = target.product_id
            existing_target_aliases.add(key)
            moved_aliases += 1

    old_name_alias = str(source.name).strip()
    if old_name_alias and old_name_alias != str(target.name).strip():
        conflict = session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.tenant_id == tenant_id,
                ProductAlias.member_id.is_(None),
                ProductAlias.alias_text == old_name_alias,
            )
            .limit(1)
        ).scalar_one_or_none()
        if conflict is None:
            session.add(
                ProductAlias(
                    tenant_id=tenant_id,
                    product_id=target.product_id,
                    member_id=None,
                    alias_text=old_name_alias,
                    source="personal",
                )
            )
            moved_aliases += 1

    # 4. 补全目标价格参考 + 标记源商品 merged
    if target.last_purchase_price_fen is None and source.last_purchase_price_fen is not None:
        target.last_purchase_price_fen = int(
            (Decimal(source.last_purchase_price_fen) / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    if target.last_sale_price_fen is None and source.last_sale_price_fen is not None:
        target.last_sale_price_fen = int(
            (Decimal(source.last_sale_price_fen) / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    source.status = "merged"
    source.merged_into = target.product_id
    resolve_gaps(
        session,
        tenant_id=tenant_id,
        store_id=store_id,
        kind="dup_suspect",
        ref_entity="product",
        ref_id=source.product_id,
    )
    session.flush()

    AuditService(session).record(
        tenant_id=tenant_id,
        action="product.merge",
        entity="product",
        entity_id=str(source.product_id),
        member_id=member_id,
        detail={
            "source_product_id": str(source.product_id),
            "source_name": source.name,
            "target_product_id": str(target.product_id),
            "target_name": target.name,
            "unit_factor": str(factor),
        },
    )

    return MergeOutcome(
        source_product_id=str(source.product_id),
        target_product_id=str(target.product_id),
        target_name=f"{target.name} {target.spec}".strip(),
        target_base_unit=str(target.base_unit),
        unit_factor=str(factor),
        moved_inventory_rows=moved_inv,
        moved_doc_lines=len(doc_lines),
        moved_movements=len(movements),
        moved_batches=len(batches),
        moved_aliases=moved_aliases,
    )
