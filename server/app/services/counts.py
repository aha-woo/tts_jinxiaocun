"""盘点：开盘点 → 填实盘 → 应用差异（写 count_adjust 流水并调批次/余额）。

历史单据不改；差异是新的库存变动，原因记 `count_adjust`。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.common.errors import AppError
from app.domain.doc_service import DocDraft, DocLineDraft, DocService
from app.domain.inventory_service import InventoryDelta, InventoryService
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.inventory import (
    CountLine,
    CountSession,
    Inventory,
    InventoryMovement,
    StockBatch,
)
from app.models.tenant import Tenant
from app.services import batch as batch_service
from app.services.audit import AuditService
from app.services.gaps import resolve_gaps
from app.utils.date_utils import today_in_tz
from app.utils.unit import CANONICAL_UNITS
from app.voice.number_normalizer import parse_quantity
from app.voice.phonetic import canonical_phonetic, fuzzy_score
from app.voice.unit_converter import standard_weight_ratio


def get_open_count(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID
) -> CountSession | None:
    """获取当前门店最近一张未结束（status='open'）的盘点单。"""
    return session.execute(  # type: ignore[attr-defined]
        select(CountSession)
        .where(
            CountSession.tenant_id == tenant_id,
            CountSession.store_id == store_id,
            CountSession.status == "open",
        )
        .order_by(CountSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def voice_batch_fill(
    session: object,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    count_id: uuid.UUID,
    spoken_text: str,
) -> dict[str, Any]:
    """零 LLM 语音连续报数盘点（例：「可乐 5 箱，矿泉水 12 瓶，大米剩 20 斤」）。"""
    del store_id
    lines = list(
        session.execute(  # type: ignore[attr-defined]
            select(CountLine).where(
                CountLine.tenant_id == tenant_id, CountLine.count_id == count_id
            )
        ).scalars()
    )
    if not lines:
        raise AppError("E_VALIDATION", message="盘点单没有可填写的商品行")
    line_pids = {ln.product_id for ln in lines}
    products = list(
        session.execute(  # type: ignore[attr-defined]
            select(Product).where(
                Product.tenant_id == tenant_id, Product.product_id.in_(list(line_pids))
            )
        ).scalars()
    )
    aliases = list(
        session.execute(  # type: ignore[attr-defined]
            select(ProductAlias).where(
                ProductAlias.tenant_id == tenant_id, ProductAlias.product_id.in_(list(line_pids))
            )
        ).scalars()
    )
    by_pid = {p.product_id: p for p in products}
    unit_pattern = "|".join(sorted((re.escape(u) for u in CANONICAL_UNITS), key=len, reverse=True))
    seg_re = re.compile(
        rf"^(?:盘点|清点|还有|剩(?:下|余)?|一共)?\s*(?P<name>.+?)\s*(?:还剩|剩下|还有|是|为|：|:)?\s*"
        rf"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万半]+)\s*(?P<unit>{unit_pattern})?$"
    )

    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []

    segments = [
        s.strip()
        for s in re.split(r"[，,、。；;\n]+", (spoken_text or "").strip())
        if s.strip()
    ]
    for seg in segments:
        if seg in ("盘点", "开始盘点"):
            continue
        m = seg_re.match(seg)
        if not m:
            unmatched.append(seg)
            continue
        raw_name = re.sub(r"^(?:盘点|还有|剩)\s*", "", m.group("name")).strip()
        num_text = m.group("num")
        spoken_unit = (m.group("unit") or "").strip()
        q_res = parse_quantity(num_text)
        if not q_res.ok or q_res.value is None or not raw_name:
            unmatched.append(seg)
            continue

        # 精确 → 别名 → 拼音近音匹配当前盘点单里的商品
        norm_q = raw_name.lower()
        hit: Product | None = None
        for p in products:
            if p.name.lower() == norm_q or f"{p.name}{p.spec}".lower() == norm_q:
                hit = p
                break
        if hit is None:
            for al in aliases:
                if al.alias_text.lower() == norm_q and al.product_id in by_pid:
                    hit = by_pid[al.product_id]
                    break
        if hit is None:
            q_phon = canonical_phonetic(raw_name)
            best: tuple[float, Product] | None = None
            for p in products:
                score = fuzzy_score(raw_name, p.name)
                if q_phon and canonical_phonetic(p.name) == q_phon:
                    score = max(score, 0.90)
                if score >= 0.80 and (best is None or score > best[0]):
                    best = (score, p)
            if best is not None:
                hit = best[1]

        if hit is None:
            unmatched.append(seg)
            continue

        qty_base = Decimal(str(q_res.value))
        if spoken_unit and spoken_unit != hit.base_unit:
            std = standard_weight_ratio(spoken_unit, hit.base_unit)
            if std is not None:
                qty_base = qty_base * std
            else:
                conv = session.execute(  # type: ignore[attr-defined]
                    select(UnitConversion)
                    .where(
                        UnitConversion.tenant_id == tenant_id,
                        UnitConversion.product_id == hit.product_id,
                        UnitConversion.from_unit == spoken_unit,
                        UnitConversion.to_unit == hit.base_unit,
                        UnitConversion.status == "approved",
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if conv is None:
                    unmatched.append(f"{seg}（未设 {spoken_unit}→{hit.base_unit} 换算）")
                    continue
                qty_base = qty_base * Decimal(str(conv.ratio))

        qty_str = format(qty_base.normalize(), "f") if qty_base == qty_base.to_integral() else str(qty_base)
        fill_line(
            session,
            tenant_id=tenant_id,
            count_id=count_id,
            product_id=hit.product_id,
            qty_counted=qty_str,
            reason="语音连续盘点",
        )
        matched.append(
            {
                "product_id": str(hit.product_id),
                "name": hit.name,
                "qty_counted": qty_str,
                "unit": hit.base_unit,
            }
        )

    return {"matched": matched, "unmatched": unmatched}


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
    accounting_mode: str = "per_txn"
    derived_sales_doc_id: str | None = None
    derived_sales_amount_fen: int = 0

    def as_dict(self) -> dict[str, object]:
        """JSON 形态。"""
        return {
            "count_id": self.count_id,
            "adjusted_lines": self.adjusted_lines,
            "total_qty_diff": str(self.total_qty_diff),
            "doc_id": self.doc_id,
            "accounting_mode": self.accounting_mode,
            "derived_sales_doc_id": self.derived_sales_doc_id,
            "derived_sales_amount_fen": self.derived_sales_amount_fen,
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

    tenant = session.get(Tenant, tenant_id)  # type: ignore[attr-defined]
    accounting_mode = str(tenant.accounting_mode) if tenant is not None else "per_txn"
    policy = batch_service.policy_for(session, tenant_id)
    inv_service = InventoryService(session)  # type: ignore[arg-type]
    doc_line_drafts: list[DocLineDraft] = []
    derived_sales_drafts: list[DocLineDraft] = []
    diffs_by_pid: dict[uuid.UUID, tuple[CountLine, Decimal, Decimal, Product | None]] = {}

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
        prod = session.execute(  # type: ignore[attr-defined]
            select(Product).where(
                Product.tenant_id == tenant_id, Product.product_id == line.product_id
            ).limit(1)
        ).scalar_one_or_none()
        diffs_by_pid[line.product_id] = (line, counted, diff, prod)
        unit = str(prod.base_unit) if prod is not None else "个"
        if accounting_mode == "count_derive" and diff < 0:
            qty_sold = abs(diff)
            sale_price = int(prod.last_sale_price_fen) if (prod and prod.last_sale_price_fen is not None) else None
            amt_fen = int((qty_sold * Decimal(sale_price)).quantize(Decimal("1"))) if sale_price is not None else None
            derived_sales_drafts.append(
                DocLineDraft(
                    product_id=line.product_id,
                    qty=qty_sold,
                    unit=unit,
                    qty_base=qty_sold,
                    unit_price_fen=sale_price,
                    amount_fen=amt_fen,
                    price_source="last" if sale_price is not None else None,
                    source_map={},
                )
            )
        else:
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

    doc_svc = DocService(session)  # type: ignore[arg-type]
    biz_today = datetime.strptime(today_in_tz("Asia/Shanghai"), "%Y-%m-%d").date()
    doc_id: uuid.UUID | None = None
    doc_lines_by_pid: dict[uuid.UUID, Any] = {}
    if doc_line_drafts:
        doc = doc_svc.create(
            DocDraft(
                tenant_id=tenant_id,
                store_id=store_id,
                intent_id="stock.take",
                biz_date=biz_today,
                created_by=member_id,
                source="count",
                lines=tuple(doc_line_drafts),
            )
        )
        doc_id = doc.doc_id
        count.doc_id = doc_id
        for dl in doc_svc.lines_of(tenant_id=tenant_id, doc_id=doc.doc_id):
            doc_lines_by_pid[dl.product_id] = dl

    derived_sales_doc_id: uuid.UUID | None = None
    derived_sales_amount_fen = 0
    if derived_sales_drafts:
        s_doc = doc_svc.create(
            DocDraft(
                tenant_id=tenant_id,
                store_id=store_id,
                intent_id="sales.create_multi" if len(derived_sales_drafts) > 1 else "sales.create",
                biz_date=biz_today,
                created_by=member_id,
                payment_status="已结",
                source="count",
                lines=tuple(derived_sales_drafts),
            )
        )
        derived_sales_doc_id = s_doc.doc_id
        derived_sales_amount_fen = int(s_doc.total_amount_fen)
        if count.doc_id is None:
            count.doc_id = derived_sales_doc_id
        for dl in doc_svc.lines_of(tenant_id=tenant_id, doc_id=s_doc.doc_id):
            doc_lines_by_pid[dl.product_id] = dl

    adjusted = 0
    total_diff = Decimal(0)
    derived_cogs_total = 0
    derived_cogs_complete = True
    for pid, (line, counted, diff, prod) in diffs_by_pid.items():
        adjusted += 1
        total_diff += diff
        inv_service.apply(
            tenant_id=tenant_id,
            store_id=store_id,
            deltas=[InventoryDelta(product_id=pid, qty_base_delta=diff)],
            allow_negative=True,
        )
        is_derived_sale = accounting_mode == "count_derive" and diff < 0
        dl = doc_lines_by_pid.get(pid)
        unit_cost = (
            prod.avg_cost_fen
            if (is_derived_sale and prod is not None and prod.avg_cost_fen is not None)
            else (
                prod.last_purchase_price_fen
                if (is_derived_sale and prod is not None and prod.last_purchase_price_fen is not None)
                else None
            )
        )
        if is_derived_sale:
            if unit_cost is None:
                derived_cogs_complete = False
            else:
                derived_cogs_total += int((abs(diff) * Decimal(unit_cost)).quantize(Decimal("1")))
        session.add(  # type: ignore[attr-defined]
            InventoryMovement(
                tenant_id=tenant_id,
                store_id=store_id,
                product_id=pid,
                doc_id=derived_sales_doc_id if is_derived_sale else doc_id,
                doc_line_id=dl.line_id if dl is not None else None,
                reason="sale" if is_derived_sale else "count_adjust",
                qty_base=diff,
                unit_cost_fen=unit_cost,
                cost_status="final" if (not is_derived_sale or unit_cost is not None) else "unknown",
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

    if derived_sales_doc_id is not None and derived_cogs_complete:
        from app.models.doc import DocHeader

        s_header = session.get(DocHeader, derived_sales_doc_id)  # type: ignore[attr-defined]
        if s_header is not None:
            s_header.cogs_fen = derived_cogs_total

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
            "accounting_mode": accounting_mode,
            "derived_sales_doc_id": str(derived_sales_doc_id) if derived_sales_doc_id else None,
        },
    )
    session.flush()  # type: ignore[attr-defined]
    return ApplyResult(
        count_id=str(count.count_id),
        adjusted_lines=adjusted,
        total_qty_diff=total_diff,
        doc_id=str(doc_id) if doc_id else None,
        accounting_mode=accounting_mode,
        derived_sales_doc_id=str(derived_sales_doc_id) if derived_sales_doc_id else None,
        derived_sales_amount_fen=derived_sales_amount_fen,
    )


def _consume_batches(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID, line: CountLine, qty: Decimal
) -> None:
    """盘亏：按先到期先出 / 先进先出从在库批次扣，不足记入虚拟批。"""
    batch_service.consume_batches_for_adjustment(
        session, tenant_id=tenant_id, store_id=store_id, product_id=line.product_id, qty=qty
    )


def _surplus_to_batches(
    session: object, *, tenant_id: uuid.UUID, store_id: uuid.UUID, line: CountLine, qty: Decimal
) -> None:
    """盘盈：先抵扣「未到货」虚拟批，余下新建一张「盘盈调整」批次（不改历史批次 qty_in）。"""
    batch_service.surplus_batches_for_adjustment(
        session, tenant_id=tenant_id, store_id=store_id, product_id=line.product_id, qty=qty
    )
