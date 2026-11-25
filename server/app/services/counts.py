"""盘点：开盘点 → 填实盘 → 应用差异（写 count_adjust 流水并调批次/余额）。

历史单据不改；差异是新的库存变动，原因记 `count_adjust`。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.common.errors import AppError
from app.domain.doc_service import DocDraft, DocLineDraft, DocService
from app.domain.inventory_service import InventoryDelta, InventoryService
from app.models.catalog import Product
from app.models.inventory import (
    CountLine,
    CountSession,
    Inventory,
    InventoryMovement,
    StockBatch,
)
from app.services import batch as batch_service
from app.services.audit import AuditService
from app.services.gaps import resolve_gaps


def create_count(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID, member_id: uuid.UUID
) -> CountSession:
    """开盘点：把当前库存快照成账面数。"""
    count = CountSession(
        tenant_id=tenant_id,
        store_id=store_id,
        status="open",
        started_by=member_id,
    )
    session.add(count)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    rows = list(
        session.execute(  # type: ignore[attr-defined]
            select(Inventory).where(
                Inventory.tenant_id == tenant_id, Inventory.store_id == store_id
            )
        ).scalars()
    )
    for row in rows:
        session.add(  # type: ignore[attr-defined]
            CountLine(
                tenant_id=tenant_id,
                count_id=count.count_id,
                product_id=row.product_id,
                qty_book=Decimal(row.qty_base),
            )
        )
    session.flush()  # type: ignore[attr-defined]
    return count


def fill_line(
    session: object,
    *,
    tenant_id: uuid.UUID,
    count_id: uuid.UUID,
    product_id: uuid.UUID,
    qty_counted: str,
    reason: str | None = None,
) -> CountLine:
    """填一行实盘数（已应用/已取消的盘点单不可再改）。"""
    count = session.execute(  # type: ignore[attr-defined]
        select(CountSession)
        .where(CountSession.tenant_id == tenant_id, CountSession.count_id == count_id)
        .limit(1)
    ).scalar_one_or_none()
    if count is None:
        raise AppError("E_VALIDATION", message="盘点单不存在")
    if str(count.status) != "open":
        raise AppError("E_VALIDATION", message="盘点单已结束，不能再修改实盘数")

    line = session.execute(  # type: ignore[attr-defined]
        select(CountLine)
        .where(
            CountLine.tenant_id == tenant_id,
            CountLine.count_id == count_id,
            CountLine.product_id == product_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if line is None:
        raise AppError("E_VALIDATION", message="盘点行不存在")
    try:
        value = Decimal(str(qty_counted))
    except Exception as exc:  # noqa: BLE001
        raise AppError("E_VALIDATION", message="实盘数不是有效数字") from exc
    if value < 0:
        raise AppError("E_VALIDATION", message="实盘数不能为负")
    line.qty_counted = value
    line.reason = reason
    session.flush()  # type: ignore[attr-defined]
    return line


@dataclass(frozen=True)
class ApplyResult:
    """应用结果。"""

    count_id: str
    adjusted_lines: int
    total_qty_diff: Decimal
    doc_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        """JSON 形态。"""
        return {
            "count_id": self.count_id,
            "adjusted_lines": self.adjusted_lines,
            "total_qty_diff": str(self.total_qty_diff),
            "doc_id": self.doc_id,
        }


def apply_count(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    count_id: uuid.UUID,
) -> ApplyResult:
    """应用盘点：按当前最新余额（含盘点期间进销流水）对账算差，写盘点单据与流水并调批次。"""
    stmt = (
        select(CountSession)
        .where(CountSession.tenant_id == tenant_id, CountSession.count_id == count_id)
        .limit(1)
    )
    bind = getattr(session, "bind", None)
    if getattr(getattr(bind, "dialect", None), "name", "") == "postgresql":
        stmt = stmt.with_for_update()
    count = session.execute(stmt).scalar_one_or_none()  # type: ignore[attr-defined]
    if count is None:
        raise AppError("E_VALIDATION", message="盘点单不存在")
    if str(count.status) != "open":
        raise AppError("E_VALIDATION", message="这张盘点单已经应用过")

    lines = list(
        session.execute(  # type: ignore[attr-defined]
            select(CountLine).where(
                CountLine.tenant_id == tenant_id, CountLine.count_id == count_id
            )
        ).scalars()
    )

    policy = batch_service.policy_for(session, tenant_id)
    inv_service = InventoryService(session)  # type: ignore[arg-type]
    doc_line_drafts: list[DocLineDraft] = []
    diffs_by_pid: dict[uuid.UUID, tuple[CountLine, Decimal, Decimal]] = {}

    for line in lines:
        if line.qty_counted is None:
            continue
        current_inv = inv_service.get_qty_base(
            tenant_id=tenant_id, store_id=store_id, product_id=line.product_id
        )
        current_qty = Decimal(current_inv.qty_base) if current_inv is not None else Decimal(0)
        counted = Decimal(line.qty_counted)
        # 差额相对「此时当前库存」计算，自动回补盘点期间发生的进货/卖货流水
        diff = counted - current_qty
        if diff == 0:
            if counted >= 0:
                resolve_gaps(
                    session,  # type: ignore[arg-type]
                    tenant_id=tenant_id,
                    store_id=store_id,
                    kind="negative_stock",
                    ref_entity="product",
                    ref_id=line.product_id,
                )
            continue
        diffs_by_pid[line.product_id] = (line, counted, diff)
        prod = session.execute(  # type: ignore[attr-defined]
            select(Product).where(
                Product.tenant_id == tenant_id, Product.product_id == line.product_id
            ).limit(1)
        ).scalar_one_or_none()
        unit = str(prod.base_unit) if prod is not None else "个"
        doc_line_drafts.append(
            DocLineDraft(
                product_id=line.product_id,
                qty=abs(diff),
                unit=unit,
                qty_base=abs(diff),
                unit_price_fen=None,
                amount_fen=None,
                price_source=None,
                source_map={},
            )
        )

    doc_id: uuid.UUID | None = None
    doc_lines_by_pid: dict[uuid.UUID, Any] = {}
    if doc_line_drafts:
        doc_svc = DocService(session)  # type: ignore[arg-type]
        doc = doc_svc.create(
            DocDraft(
                tenant_id=tenant_id,
                store_id=store_id,
                intent_id="stock.take",
                biz_date=datetime.now(timezone.utc).date(),
                created_by=member_id,
                source="count",
                lines=tuple(doc_line_drafts),
            )
        )
        doc_id = doc.doc_id
        count.doc_id = doc_id
        for dl in doc_svc.lines_of(tenant_id=tenant_id, doc_id=doc.doc_id):
            doc_lines_by_pid[dl.product_id] = dl

    adjusted = 0
    total_diff = Decimal(0)
    for pid, (line, counted, diff) in diffs_by_pid.items():
        adjusted += 1
        total_diff += diff
        inv_service.apply(
            tenant_id=tenant_id,
            store_id=store_id,
            deltas=[InventoryDelta(product_id=pid, qty_base_delta=diff)],
            allow_negative=True,
        )
        dl = doc_lines_by_pid.get(pid)
        session.add(  # type: ignore[attr-defined]
            InventoryMovement(
                tenant_id=tenant_id,
                store_id=store_id,
                product_id=pid,
                doc_id=doc_id,
                doc_line_id=dl.line_id if dl is not None else None,
                reason="count_adjust",
                qty_base=diff,
                unit_cost_fen=None,
                cost_status="final",
                partner_id=None,
                created_by=member_id,
            )
        )
        if batch_service.product_tracks_batch(session, tenant_id, pid, policy):
            if diff < 0:
                _consume_batches(session, tenant_id=tenant_id, store_id=store_id, line=line, qty=-diff)
            else:
                _surplus_to_batches(session, tenant_id=tenant_id, store_id=store_id, line=line, qty=diff)
        if counted >= 0:
            resolve_gaps(
                session,  # type: ignore[arg-type]
                tenant_id=tenant_id,
                store_id=store_id,
                kind="negative_stock",
                ref_entity="product",
                ref_id=pid,
            )

    count.status = "applied"
    count.applied_at = datetime.now(timezone.utc)
    AuditService(session).record(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        action="count.apply",
        entity="count_session",
        entity_id=str(count_id),
        member_id=member_id,
        detail={
            "adjusted_lines": adjusted,
            "total_qty_diff": str(total_diff),
            "doc_id": str(doc_id) if doc_id else None,
        },
    )
    session.flush()  # type: ignore[attr-defined]
    return ApplyResult(
        count_id=str(count.count_id),
        adjusted_lines=adjusted,
        total_qty_diff=total_diff,
        doc_id=str(doc_id) if doc_id else None,
    )


def _consume_batches(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID, line: CountLine, qty: Decimal
) -> None:
    """盘亏：按先到期先出 / 先进先出从在库批次扣。"""
    batches = list(
        session.execute(  # type: ignore[attr-defined]
            select(StockBatch)
            .where(
                StockBatch.tenant_id == tenant_id,
                StockBatch.store_id == store_id,
                StockBatch.product_id == line.product_id,
                StockBatch.status == "open",
                StockBatch.is_virtual.is_(False),
                StockBatch.qty_left > 0,
            )
            .order_by(StockBatch.expiry_date.is_(None), StockBatch.expiry_date, StockBatch.received_at, StockBatch.batch_id)
        ).scalars()
    )
    left = qty
    for batch in batches:
        if left <= 0:
            break
        take = min(Decimal(batch.qty_left), left)
        batch.qty_left = Decimal(batch.qty_left) - take
        if Decimal(batch.qty_left) <= 0:
            batch.qty_left = Decimal(0)
            batch.status = "exhausted"
        left -= take


def _surplus_to_batches(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID, line: CountLine, qty: Decimal
) -> None:
    """盘盈：先抵扣「未到货」虚拟批，余下新建一张「盘盈调整」批次（不改历史批次 qty_in）。"""
    rem = qty
    vbatch = session.execute(  # type: ignore[attr-defined]
        select(StockBatch)
        .where(
            StockBatch.tenant_id == tenant_id,
            StockBatch.store_id == store_id,
            StockBatch.product_id == line.product_id,
            StockBatch.is_virtual.is_(True),
            StockBatch.status == "open",
        )
        .limit(1)
    ).scalar_one_or_none()
    if vbatch is not None and Decimal(vbatch.qty_left) < 0:
        repay = min(rem, abs(Decimal(vbatch.qty_left)))
        vbatch.qty_left = Decimal(vbatch.qty_left) + repay
        if Decimal(vbatch.qty_left) >= 0:
            vbatch.qty_left = Decimal(0)
            vbatch.status = "exhausted"
        rem -= repay

    if rem <= 0:
        return
    product = session.execute(  # type: ignore[attr-defined]
        select(Product).where(
            Product.tenant_id == tenant_id, Product.product_id == line.product_id
        ).limit(1)
    ).scalar_one_or_none()
    unit_cost = (
        product.avg_cost_fen if (product and product.avg_cost_fen is not None)
        else (product.last_purchase_price_fen if product else None)
    )
    session.add(  # type: ignore[attr-defined]
        StockBatch(
            tenant_id=tenant_id,
            store_id=store_id,
            product_id=line.product_id,
            doc_line_id=None,
            partner_id=None,
            label="盘盈调整",
            is_virtual=False,
            qty_in=rem,
            qty_left=rem,
            unit_cost_fen=unit_cost,
            status="open",
            received_at=datetime.now(timezone.utc),
        )
    )
