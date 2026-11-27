"""批次与成本法（M0 定稿口径）：移动加权平均（默认）+ 商品级批次实际成本（FEFO/FIFO）。

核心规则
--------
1. 成本法收敛为 ``avg``（移动加权平均，默认）与 ``batch``（按批次实际成本，自动 FEFO/FIFO；
   兼容历史 ``fifo`` / ``fefe`` / ``fefo``）；
2. 进货未说进价时 ``unit_cost_fen = NULL``（不参与移动平均，绝不用 0 拉低均价）；
3. 默认允许负库存：超卖数量按最近成本暂估（``provisional`` / 未知 ``unknown``），
   批次商品挂在「未到货」虚拟批次（``is_virtual=True``）；
4. 补进货时自动抵扣虚拟批次并按本次真实进价结清暂估流水，写入 ``cost_adjustment``；
5. 商品关闭 ``track_batch`` 时不强制建物理批次，仅维护移动平均与流水。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.catalog import Product
from app.models.doc import DocHeader, DocLine
from app.models.inventory import (
    CostAdjustment,
    Inventory,
    InventoryMovement,
    InventoryPolicy,
    StockBatch,
)
from app.models.tenant import Tenant
from app.services.gaps import resolve_gaps
from app.utils.date_utils import today_in_tz

_BATCH_METHODS = frozenset({"batch", "fifo", "fefe", "fefo"})


@dataclass(frozen=True)
class BatchAllocation:
    """一次出库从某个批次（或移动平均槽位）扣多少。"""

    batch_id: uuid.UUID | None
    qty_base: Decimal
    unit_cost_fen: int | None
    cost_status: str = "final"
    is_virtual: bool = False


@dataclass(frozen=True)
class StockPolicy:
    """生效的租户库存策略。"""

    cost_method: str
    allow_negative: bool


def _is_pg(session: object) -> bool:
    bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def _lock_stmt(statement: Any, session: object, *, for_update: bool) -> Any:
    if for_update and _is_pg(session) and hasattr(statement, "with_for_update"):
        return statement.with_for_update()
    return statement


def policy_for(session: object, tenant_id: uuid.UUID) -> StockPolicy:
    """租户策略；缺省默认 ``avg`` + 读取 ``Tenant.allow_negative``（默认 True）。"""
    row = session.execute(  # type: ignore[attr-defined]
        select(InventoryPolicy).where(InventoryPolicy.tenant_id == tenant_id).limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return StockPolicy(
            cost_method=str(row.cost_method),
            allow_negative=bool(row.allow_negative),
        )
    tenant = session.execute(  # type: ignore[attr-defined]
        select(Tenant).where(Tenant.tenant_id == tenant_id).limit(1)
    ).scalar_one_or_none()
    return StockPolicy(
        cost_method="avg",
        allow_negative=bool(tenant.allow_negative) if tenant is not None else True,
    )


def _get_product(session: object, tenant_id: uuid.UUID, product_id: uuid.UUID) -> Product | None:
    return session.execute(  # type: ignore[attr-defined]
        select(Product)
        .where(Product.tenant_id == tenant_id, Product.product_id == product_id)
        .limit(1)
    ).scalar_one_or_none()


def method_for(
    session: object, tenant_id: uuid.UUID, product_id: uuid.UUID, fallback: StockPolicy
) -> str:
    """商品成本法优先；开启批次且未单设成本法时取 ``batch``。"""
    product = _get_product(session, tenant_id, product_id)
    if product is not None:
        if product.cost_method:
            return str(product.cost_method)
        if bool(product.track_batch):
            return "batch"
    return fallback.cost_method


def product_tracks_batch(
    session: object, tenant_id: uuid.UUID, product_id: uuid.UUID, fallback: StockPolicy
) -> bool:
    """该商品是否启用批次管理（商品开关优先；全局选了批次/FIFO/FEFO 也开启）。"""
    product = _get_product(session, tenant_id, product_id)
    if product is not None:
        if bool(product.track_batch):
            return True
        if product.cost_method in _BATCH_METHODS:
            return True
        if product.cost_method == "avg":
            return False
    return fallback.cost_method in _BATCH_METHODS


def effective_allow_negative(
    session: object, tenant_id: uuid.UUID, product_id: uuid.UUID, fallback: StockPolicy
) -> bool:
    """商品级 ``allow_negative`` 覆盖租户默认。"""
    product = _get_product(session, tenant_id, product_id)
    if product is not None and product.allow_negative is not None:
        return bool(product.allow_negative)
    return fallback.allow_negative


def create_batch(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    line: DocLine,
    partner_id: uuid.UUID | None,
    partner_name: str | None = None,
    biz_date: date | None = None,
    expiry_date: date | None = None,
    label: str | None = None,
) -> StockBatch:
    """入库行 → 批次；成本按基本单位换算，未写金额时 ``unit_cost_fen=None``。"""
    qty_base = Decimal(line.qty_base)
    unit_cost: int | None
    if line.amount_fen is not None and qty_base > 0:
        unit_cost = int(
            (Decimal(line.amount_fen) / qty_base).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
    elif line.unit_price_fen is not None and Decimal(line.qty) == qty_base:
        unit_cost = int(line.unit_price_fen)
    else:
        unit_cost = None

    day = biz_date or date.fromisoformat(today_in_tz("Asia/Shanghai"))
    auto_label = label or (
        f"{day.month}/{day.day} {partner_name}" if partner_name else f"{day.month}/{day.day} 进货"
    )
    batch = StockBatch(
        tenant_id=tenant_id,
        store_id=store_id,
        product_id=line.product_id,
        doc_line_id=line.line_id,
        partner_id=partner_id,
        label=auto_label[:64],
        is_virtual=False,
        qty_in=qty_base,
        qty_left=qty_base,
        unit_cost_fen=unit_cost,
        expiry_date=expiry_date,
        status="open",
        # 显式带微秒，防止同秒 FIFO 顺序退化
        received_at=datetime.now(timezone.utc),
    )
    session.add(batch)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    return batch


def _open_batches(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    product_id: uuid.UUID,
    method: str,
    include_virtual: bool = False,
    for_update: bool = False,
) -> list[StockBatch]:
    statement = select(StockBatch).where(
        StockBatch.tenant_id == tenant_id,
        StockBatch.store_id == store_id,
        StockBatch.product_id == product_id,
        StockBatch.status == "open",
        StockBatch.qty_left > 0,
    )
    if not include_virtual:
        statement = statement.where(StockBatch.is_virtual.is_(False))
    if method in {"fefe", "fefo", "batch"}:
        statement = statement.order_by(
            StockBatch.expiry_date.is_(None), StockBatch.expiry_date, StockBatch.received_at, StockBatch.batch_id
        )
    else:
        statement = statement.order_by(StockBatch.received_at, StockBatch.batch_id)
    statement = _lock_stmt(statement, session, for_update=for_update)
    return list(session.execute(statement).scalars())  # type: ignore[attr-defined]


def _get_or_create_virtual_batch(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    product_id: uuid.UUID,
) -> StockBatch:
    statement = (
        select(StockBatch)
        .where(
            StockBatch.tenant_id == tenant_id,
            StockBatch.store_id == store_id,
            StockBatch.product_id == product_id,
            StockBatch.is_virtual.is_(True),
            StockBatch.status == "open",
        )
        .limit(1)
    )
    existing = session.execute(_lock_stmt(statement, session, for_update=True)).scalar_one_or_none()  # type: ignore[attr-defined]
    if existing is not None:
        return existing
    batch = StockBatch(
        tenant_id=tenant_id,
        store_id=store_id,
        product_id=product_id,
        doc_line_id=None,
        partner_id=None,
        label="未到货",
        is_virtual=True,
        qty_in=Decimal(0),
        qty_left=Decimal(0),
        unit_cost_fen=None,
        status="open",
        received_at=datetime.now(timezone.utc),
    )
    session.add(batch)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    return batch


def update_moving_avg_on_purchase(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    product_id: uuid.UUID,
    qty_before: Decimal,
    qty_in: Decimal,
    unit_cost_fen: int | None,
) -> int | None:
    """进货后更新移动加权平均成本。``unit_cost_fen=None``（缺进价）绝不按 0 稀释均价。"""
    product = _get_product(session, tenant_id, product_id)
    inv = session.execute(  # type: ignore[attr-defined]
        select(Inventory)
        .where(
            Inventory.tenant_id == tenant_id,
            Inventory.store_id == store_id,
            Inventory.product_id == product_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    if unit_cost_fen is None or qty_in <= 0:
        if inv is not None and inv.avg_cost_fen is not None:
            return int(inv.avg_cost_fen)
        return int(product.avg_cost_fen) if (product and product.avg_cost_fen is not None) else None

    old_avg: int | None = None
    if inv is not None and inv.avg_cost_fen is not None:
        old_avg = int(inv.avg_cost_fen)
    elif product is not None and product.avg_cost_fen is not None:
        old_avg = int(product.avg_cost_fen)
    elif product is not None and product.last_purchase_price_fen is not None:
        old_avg = int(product.last_purchase_price_fen)

    if qty_before <= 0 or old_avg is None:
        new_avg = int(unit_cost_fen)
    else:
        total_qty = qty_before + qty_in
        total_val = qty_before * Decimal(old_avg) + qty_in * Decimal(unit_cost_fen)
        new_avg = int((total_val / total_qty).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    if inv is not None:
        inv.avg_cost_fen = new_avg
    if product is not None:
        product.avg_cost_fen = new_avg
        product.last_purchase_price_fen = int(unit_cost_fen)
        if str(product.status) == "draft" and product.last_sale_price_fen is not None:
            product.status = "active"
    session.flush()  # type: ignore[attr-defined]
    return new_avg


def _provisional_cost_for(product: Product | None, inv: Inventory | None) -> int | None:
    if inv is not None and inv.avg_cost_fen is not None:
        return int(inv.avg_cost_fen)
    if product is not None:
        if product.avg_cost_fen is not None:
            return int(product.avg_cost_fen)
        if product.last_purchase_price_fen is not None:
            return int(product.last_purchase_price_fen)
    return None


def plan_sale(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    product_id: uuid.UUID,
    qty_base: Decimal,
    policy: StockPolicy,
) -> list[BatchAllocation]:
    """算出这次出库从哪些批次（或移动平均）扣；不足时按 ``allow_negative`` 拦截或走暂估+虚拟批。"""
    if qty_base <= 0:
        return []

    allow_neg = effective_allow_negative(session, tenant_id, product_id, policy)
    tracks = product_tracks_batch(session, tenant_id, product_id, policy)
    method = method_for(session, tenant_id, product_id, policy)
    product = _get_product(session, tenant_id, product_id)
    inv = session.execute(  # type: ignore[attr-defined]
        _lock_stmt(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id,
                Inventory.store_id == store_id,
                Inventory.product_id == product_id,
            ).limit(1),
            session,
            for_update=True,
        )
    ).scalar_one_or_none()
    provisional_cost = _provisional_cost_for(product, inv)

    if not tracks:
        current_qty = Decimal(inv.qty_base) if inv is not None else Decimal(0)
        available = max(Decimal(0), current_qty)
        if available < qty_base and not allow_neg:
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"库存不足：可卖 {available}，这次要 {qty_base}。"
                    "先记一笔进货，或在库存设置里允许负库存"
                ),
                data={"reason": "negative_stock", "product_id": str(product_id)},
            )
        allocations: list[BatchAllocation] = []
        take_onhand = min(available, qty_base)
        shortage = qty_base - take_onhand
        if take_onhand > 0:
            allocations.append(
                BatchAllocation(
                    batch_id=None,
                    qty_base=take_onhand,
                    unit_cost_fen=provisional_cost,
                    cost_status="final" if provisional_cost is not None else "unknown",
                )
            )
        if shortage > 0:
            allocations.append(
                BatchAllocation(
                    batch_id=None,
                    qty_base=shortage,
                    unit_cost_fen=provisional_cost,
                    cost_status="provisional" if provisional_cost is not None else "unknown",
                )
            )
        return allocations

    batches = _open_batches(
        session=session,
        tenant_id=tenant_id,
        store_id=store_id,
        product_id=product_id,
        method=method,
        include_virtual=False,
        for_update=True,
    )
    available = sum((Decimal(batch.qty_left) for batch in batches), Decimal(0))
    if available < qty_base and not allow_neg:
        raise AppError(
            "E_VALIDATION",
            message=(
                f"库存不足：可卖 {available}，这次要 {qty_base}。"
                "先记一笔进货，或在库存设置里允许负库存"
            ),
            data={"reason": "negative_stock", "product_id": str(product_id)},
        )

    avg_batch_cost: int | None = None
    if method == "avg" and available > 0:
        priced = [b for b in batches if b.unit_cost_fen is not None]
        priced_qty = sum((Decimal(b.qty_left) for b in priced), Decimal(0))
        if priced_qty > 0:
            total_value = sum(
                (Decimal(b.qty_left) * Decimal(int(b.unit_cost_fen or 0)) for b in priced),
                Decimal(0),
            )
            avg_batch_cost = int((total_value / priced_qty).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    allocations = []
    left = qty_base
    for batch in batches:
        if left <= 0:
            break
        take = min(Decimal(batch.qty_left), left)
        if method == "avg":
            cost = avg_batch_cost
        else:
            cost = int(batch.unit_cost_fen) if batch.unit_cost_fen is not None else None
        allocations.append(
            BatchAllocation(
                batch_id=batch.batch_id,
                qty_base=take,
                unit_cost_fen=cost,
                cost_status="final" if cost is not None else "unknown",
                is_virtual=False,
            )
        )
        left -= take
    if left > 0:
        vbatch = _get_or_create_virtual_batch(
            session, tenant_id=tenant_id, store_id=store_id, product_id=product_id
        )
        fallback_cost = provisional_cost
        if fallback_cost is None:
            for b in reversed(batches):
                if b.unit_cost_fen is not None:
                    fallback_cost = int(b.unit_cost_fen)
                    break
        allocations.append(
            BatchAllocation(
                batch_id=vbatch.batch_id,
                qty_base=left,
                unit_cost_fen=fallback_cost,
                cost_status="provisional" if fallback_cost is not None else "unknown",
                is_virtual=True,
            )
        )
    return allocations


def apply_sale(session: object, allocations: list[BatchAllocation]) -> None:
    """按分配扣批次余量（真实批次不得静默归零；虚拟「未到货」批次保留负数缺口）。"""
    for item in allocations:
        if item.batch_id is None:
            continue
        batch = session.get(StockBatch, item.batch_id)  # type: ignore[attr-defined]
        if batch is None:
            continue
        new_left = Decimal(batch.qty_left) - item.qty_base
        if bool(batch.is_virtual):
            batch.qty_left = new_left
            continue
        if new_left < 0:
            raise AppError(
                "E_PREVIEW_VERSION_CONFLICT",
                message="批次余量已被并发修改，请重新打开卡片再确认",
                data={"batch_id": str(batch.batch_id)},
            )
        batch.qty_left = new_left
        if new_left == 0:
            batch.status = "exhausted"
    session.flush()  # type: ignore[attr-defined]


def settle_oversold_on_purchase(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    product_id: uuid.UUID,
    source_doc_id: uuid.UUID,
    qty_before: Decimal,
    qty_in: Decimal,
    unit_cost_fen: int | None,
    new_batch: StockBatch | None = None,
) -> int:
    """补进货时自动抵扣负库存/虚拟批次，并将暂估销售流水结清为真实进价，写 ``cost_adjustment``。"""
    if qty_in <= 0:
        return 0

    settled_qty = min(qty_in, abs(qty_before)) if qty_before < 0 else Decimal(0)

    # 1) 抵扣「未到货」虚拟批次 + 对应新批余量
    if settled_qty > 0:
        vbatch = session.execute(
            select(StockBatch)
            .where(
                StockBatch.tenant_id == tenant_id,
                StockBatch.store_id == store_id,
                StockBatch.product_id == product_id,
                StockBatch.is_virtual.is_(True),
                StockBatch.status == "open",
            )
            .limit(1)
        ).scalar_one_or_none()
        if vbatch is not None:
            deficit = abs(min(Decimal(0), Decimal(vbatch.qty_left)))
            repay = min(settled_qty, deficit) if deficit > 0 else settled_qty
            vbatch.qty_left = Decimal(vbatch.qty_left) + repay
            if Decimal(vbatch.qty_left) >= 0:
                vbatch.qty_left = Decimal(0)
                vbatch.status = "exhausted"
            if new_batch is not None and repay > 0:
                new_left = max(Decimal(0), Decimal(new_batch.qty_left) - repay)
                new_batch.qty_left = new_left
                if new_left == 0:
                    new_batch.status = "exhausted"

    # 2) 若本次进价已知，结清过往暂估/未知的销售流水与行成本
    adjustments = 0
    if settled_qty > 0 and unit_cost_fen is not None:
        moves = list(
            session.execute(
                select(InventoryMovement)
                .where(
                    InventoryMovement.tenant_id == tenant_id,
                    InventoryMovement.store_id == store_id,
                    InventoryMovement.product_id == product_id,
                    InventoryMovement.reason == "sale",
                    InventoryMovement.cost_status.in_(("provisional", "unknown")),
                )
                .order_by(InventoryMovement.occurred_at, InventoryMovement.move_id)
            ).scalars()
        )
        left_to_settle = settled_qty
        touched_docs: set[uuid.UUID] = set()
        for move in moves:
            if left_to_settle <= 0:
                break
            sold_qty = abs(Decimal(move.qty_base))
            portion = min(sold_qty, left_to_settle)
            old_cost = move.unit_cost_fen
            delta_cogs = int(
                (
                    (Decimal(unit_cost_fen) - Decimal(int(old_cost or 0))) * portion
                ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            session.add(
                CostAdjustment(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=product_id,
                    sale_doc_id=move.doc_id,
                    sale_line_id=move.doc_line_id,
                    source_doc_id=source_doc_id,
                    qty_base=portion,
                    old_unit_cost_fen=int(old_cost) if old_cost is not None else None,
                    new_unit_cost_fen=int(unit_cost_fen),
                    delta_cogs_fen=delta_cogs,
                    reason="oversold_settle",
                )
            )
            move.unit_cost_fen = int(unit_cost_fen)
            move.cost_status = "final"
            if move.doc_line_id is not None:
                line = session.get(DocLine, move.doc_line_id)
                if line is not None:
                    line.cost_snapshot_fen = int(
                        (Decimal(int(unit_cost_fen)) * Decimal(line.qty_base)).quantize(
                            Decimal(1), rounding=ROUND_HALF_UP
                        )
                    )
                    line.cost_status = "final"
            if move.doc_id is not None:
                touched_docs.add(move.doc_id)
            left_to_settle -= portion
            adjustments += 1

        # 刷新受影响销售单的表头 COGS
        for sale_doc_id in touched_docs:
            header = session.get(DocHeader, sale_doc_id)
            if header is None:
                continue
            doc_moves = list(
                session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.doc_id == sale_doc_id,
                        InventoryMovement.reason == "sale",
                    )
                ).scalars()
            )
            if doc_moves and all(m.unit_cost_fen is not None for m in doc_moves):
                total_cogs = sum(
                    abs(Decimal(m.qty_base)) * Decimal(int(m.unit_cost_fen or 0))
                    for m in doc_moves
                )
                header.cogs_fen = int(total_cogs.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    # 3) 补正负库存待处理
    if qty_before + qty_in >= 0:
        resolve_gaps(
            session,
            tenant_id=tenant_id,
            store_id=store_id,
            kind="negative_stock",
            ref_entity="product",
            ref_id=product_id,
        )
    session.flush()
    return adjustments


def restore_on_reversal(
    session: object, *, tenant_id: uuid.UUID, doc_id: uuid.UUID
) -> None:
    """反做时恢复批次：进货批次标记 adjusted，卖货按流水把量还回去（含虚拟批）。"""
    moves = list(
        session.execute(  # type: ignore[attr-defined]
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id, InventoryMovement.doc_id == doc_id
            )
        ).scalars()
    )
    for move in moves:
        if move.batch_id is None:
            continue
        batch = session.get(StockBatch, move.batch_id)  # type: ignore[attr-defined]
        if batch is None:
            continue
        if move.reason == "purchase":
            consumed = Decimal(batch.qty_in) - Decimal(batch.qty_left)
            batch.status = "adjusted"
            if consumed > 0:
                vbatch = _get_or_create_virtual_batch(
                    session,
                    tenant_id=tenant_id,
                    store_id=batch.store_id,
                    product_id=batch.product_id,
                )
                vbatch.qty_left = Decimal(vbatch.qty_left) - consumed
        elif move.reason == "sale":
            # move.qty_base 为负，减去负数 = 加回余量
            batch.qty_left = Decimal(batch.qty_left) - Decimal(move.qty_base)
            if bool(batch.is_virtual):
                if Decimal(batch.qty_left) >= 0:
                    batch.qty_left = Decimal(0)
                    batch.status = "exhausted"
            elif Decimal(batch.qty_left) > 0 and batch.status == "exhausted":
                batch.status = "open"
    session.flush()  # type: ignore[attr-defined]


def cogs_detail_of(allocations: list[BatchAllocation]) -> tuple[int | None, str]:
    """返回 ``(cogs_fen, cost_status)``；任一行未知成本时返回 ``(None, 'unknown')``。"""
    if not allocations:
        return 0, "final"
    if any(item.unit_cost_fen is None for item in allocations):
        return None, "unknown"
    total = Decimal(0)
    status = "final"
    for item in allocations:
        if item.cost_status == "provisional":
            status = "provisional"
        total += Decimal(item.qty_base) * Decimal(int(item.unit_cost_fen or 0))
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP)), status


def cogs_of(allocations: list[BatchAllocation]) -> int | None:
    """这次卖货的销售成本（分）；未知成本返回 ``None``。"""
    cogs, _ = cogs_detail_of(allocations)
    return cogs


def reconcile_store(
    session: Session, *, tenant_id: uuid.UUID, store_id: uuid.UUID
) -> dict[str, Any]:
    """只读对账：1) 库存余额 == 流水合计；2) 批次商品批次余量 == 库存余额；3) 未结清暂估流水。"""
    inv_rows = list(
        session.execute(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id, Inventory.store_id == store_id
            )
        ).scalars()
    )
    move_rows = session.execute(
        select(InventoryMovement.product_id, func.coalesce(func.sum(InventoryMovement.qty_base), 0))
        .where(InventoryMovement.tenant_id == tenant_id, InventoryMovement.store_id == store_id)
        .group_by(InventoryMovement.product_id)
    ).all()
    move_sum_by_prod = {pid: Decimal(total) for pid, total in move_rows}

    products = {
        p.product_id: p
        for p in session.execute(select(Product).where(Product.tenant_id == tenant_id)).scalars()
    }
    policy = policy_for(session, tenant_id)

    balance_mismatches: list[dict[str, Any]] = []
    batch_mismatches: list[dict[str, Any]] = []
    seen_pids = {row.product_id for row in inv_rows} | set(move_sum_by_prod)

    inv_by_pid = {row.product_id: Decimal(row.qty_base) for row in inv_rows}
    for pid in seen_pids:
        bal = inv_by_pid.get(pid, Decimal(0))
        msum = move_sum_by_prod.get(pid, Decimal(0))
        if bal != msum:
            balance_mismatches.append(
                {
                    "product_id": str(pid),
                    "product_name": products[pid].name if pid in products else "",
                    "balance_qty": str(bal),
                    "movement_sum": str(msum),
                }
            )
        if product_tracks_batch(session, tenant_id, pid, policy):
            batch_sum = Decimal(
                session.execute(
                    select(func.coalesce(func.sum(StockBatch.qty_left), 0)).where(
                        StockBatch.tenant_id == tenant_id,
                        StockBatch.store_id == store_id,
                        StockBatch.product_id == pid,
                        StockBatch.status != "adjusted",
                    )
                ).scalar_one()
                or 0
            )
            if bal != batch_sum:
                batch_mismatches.append(
                    {
                        "product_id": str(pid),
                        "product_name": products[pid].name if pid in products else "",
                        "balance_qty": str(bal),
                        "batch_sum": str(batch_sum),
                    }
                )

    unsettled_count = int(
        session.execute(
            select(func.count())
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.store_id == store_id,
                InventoryMovement.cost_status.in_(("provisional", "unknown")),
            )
        ).scalar_one()
        or 0
    )

    return {
        "ok": not balance_mismatches and not batch_mismatches,
        "balance_mismatches": balance_mismatches,
        "batch_mismatches": batch_mismatches,
        "unsettled_cost_movements": unsettled_count,
    }
