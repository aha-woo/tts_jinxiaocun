"""撤销 = **反做单**：原单保留不删（PRD 3.8 / 验收⑤）。

为什么是反做单而不是删除
----------------------
删掉一笔单，账面上「今天卖了 300」会平白少一笔，事后谁也不知道发生过什么；
而反做单是**再记一笔反向的**，两笔都在、都能查、都能导出。审计上看得见，
对账时能算平，老板也不会问「我那单哪去了」。

三条约束
-------
1. **窗口**：契约 ``intent.reversal.window_minutes``（默认 10 分钟）；
   超窗 → ``E_REVERSAL_WINDOW_CLOSED``(1203)，引导走 ``sys.correct`` 更正流程；
2. **被引用就拒**：``reversal.block_if_referenced=true`` 的单据，一旦被别的单据引用
   （退货单、收付款核销、反做单）→ ``E_DOC_REFERENCED``(1204)，并**展示引用链**；
3. **R0 不存在撤销**（没写过，撤什么）——直接拒。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.domain.ar_ap_service import ArApDelta, ArApService
from app.domain.doc_service import DocDraft, DocLineDraft, DocService, REVERSAL_SUFFIX
from app.domain.intent_registry import IntentRegistry, default_registry
from app.domain.inventory_service import InventoryDelta, InventoryRow, InventoryService
from app.models.doc import ArAp, DocHeader, DocLine, Payment
from app.models.dict import Reversal
from app.models.inventory import InventoryMovement
from app.services import batch as batch_service
from app.services.audit import AuditService
from app.services.movement import reason_of, record_doc_movements
from app.services.batch import restore_on_reversal

__all__ = [
    "CorrectionOutcome",
    "DEFAULT_REVERSAL_WINDOW_MINUTES",
    "ReferenceLink",
    "ReversalOutcome",
    "ReversalService",
]

#: 撤销窗口默认值（契约缺省时用；【假设】）
DEFAULT_REVERSAL_WINDOW_MINUTES = 10


@dataclass(frozen=True)
class ReferenceLink:
    """引用链上的一环（``E_DOC_REFERENCED`` 时展示给用户看）。"""

    entity: str
    entity_id: str
    label: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "entity": self.entity,
            "entity_id": self.entity_id,
            "label": self.label,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class ReversalOutcome:
    """撤销结果。"""

    original_doc_id: str
    reversal_doc_id: str | None = None
    reversal_doc_no: str | None = None
    idempotent: bool = False
    inventory_after: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "original_doc_id": self.original_doc_id,
            "reversal_doc_id": self.reversal_doc_id,
            "reversal_doc_no": self.reversal_doc_no,
            "idempotent": self.idempotent,
            "inventory_after": dict(self.inventory_after),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CorrectionOutcome:
    """一步改单结果（同一事务：反做原单 + 新建更正单）。"""

    original_doc_id: str
    reversal_doc_id: str
    new_doc_id: str
    new_doc_no: str
    total_amount_fen: int
    inventory_after: Mapping[str, str] = field(default_factory=dict)
    detail: str = "已更正：原单已反做，新单已生效"

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "original_doc_id": self.original_doc_id,
            "reversal_doc_id": self.reversal_doc_id,
            "new_doc_id": self.new_doc_id,
            "new_doc_no": self.new_doc_no,
            "total_amount_fen": self.total_amount_fen,
            "inventory_after": dict(self.inventory_after),
            "detail": self.detail,
        }


class ReversalService:
    """反做单服务。"""

    def __init__(
        self,
        session: Session,
        *,
        registry: IntentRegistry | None = None,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（写事务）。
            registry: 意图注册表（撤销窗口/是否可撤一律从这里读）。
        """
        self._session = session
        self._registry = registry or default_registry()
        self._audit = AuditService(session)

    # ------------------------------------------------------------------ 查询
    def window_minutes_of(self, intent_id: str) -> int:
        """该意图的撤销窗口（分钟）；不可撤销返回 0。"""
        spec = self._registry.get(intent_id)
        if spec is None:
            return 0
        reversal = dict(spec.reversal or {})
        if not reversal.get("allowed", False):
            return 0
        value = reversal.get("window_minutes")
        return int(value) if value else DEFAULT_REVERSAL_WINDOW_MINUTES

    def references_of(self, *, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> list[ReferenceLink]:
        """列出引用这张单的所有对象（引用链）。"""
        links: list[ReferenceLink] = []

        # 1) 应收应付挂在这张单上，且已经有核销金额
        ar_rows = list(
            self._session.execute(
                select(ArAp).where(ArAp.tenant_id == tenant_id, ArAp.doc_id == doc_id)
            ).scalars()
        )
        for ar_ap in ar_rows:
            if int(ar_ap.settled_amount_fen) > 0:
                links.append(
                    ReferenceLink(
                        entity="ar_ap",
                        entity_id=str(ar_ap.ar_ap_id),
                        label=f"{ar_ap.direction} 已核销 {ar_ap.settled_amount_fen} 分",
                        extra={"direction": str(ar_ap.direction)},
                    )
                )

        # 2) 反做单已经存在（重复撤销 / 已被别的流程反做）
        for reversal in self._session.execute(
            select(Reversal).where(
                Reversal.tenant_id == tenant_id, Reversal.original_doc_id == doc_id
            )
        ).scalars():
            links.append(
                ReferenceLink(
                    entity="reversal",
                    entity_id=str(reversal.reversal_doc_id),
                    label="已有反做单",
                )
            )

        # 3) 收付款流水挂在它的应收应付上
        for ar_ap in ar_rows:
            payments = list(
                self._session.execute(
                    select(Payment).where(
                        Payment.tenant_id == tenant_id, Payment.ar_ap_id == ar_ap.ar_ap_id
                    )
                ).scalars()
            )
            for payment in payments:
                links.append(
                    ReferenceLink(
                        entity="payment",
                        entity_id=str(payment.payment_id),
                        label=f"收付款流水 {payment.amount_fen} 分",
                    )
                )
        return links

    # ------------------------------------------------------------------ 撤销
    def reverse(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        doc_id: uuid.UUID,
        member_id: uuid.UUID,
        reason: str = "",
        now: datetime | None = None,
        ignore_window: bool = False,
    ) -> ReversalOutcome:
        """撤销一张单据（生成反做单，原单保留）。"""
        moment = now or datetime.now(timezone.utc)
        doc_service = DocService(self._session)
        doc_stmt = select(DocHeader).where(
            DocHeader.tenant_id == tenant_id, DocHeader.doc_id == doc_id
        ).limit(1)
        bind = getattr(self._session, "bind", None)
        if getattr(getattr(bind, "dialect", None), "name", "") == "postgresql":
            doc_stmt = doc_stmt.with_for_update()
        doc = self._session.execute(doc_stmt).scalar_one_or_none()
        if doc is None:
            raise AppError("E_VALIDATION", message="单据不存在或不属于当前租户")

        if doc.reverses_doc_id is not None:
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"{doc.doc_no} 本身就是一张撤销单，不能再撤销（否则会重复冲销一次）；"
                    "要改请重新录一句正确的，或用「改一下」在卡片上直接改数量/金额"
                ),
                data={
                    "reason": "cannot_reverse_a_reversal",
                    "doc_no": str(doc.doc_no),
                    "reverses_doc_id": str(doc.reverses_doc_id),
                },
            )

        spec = self._registry.get(str(doc.intent_id))
        reversal_spec = dict(spec.reversal or {}) if spec is not None else {}
        if not reversal_spec.get("allowed", False):
            raise AppError(
                "E_VALIDATION",
                message=f"{doc.intent_id} 不支持撤销（R0 只读动作不存在撤销；R3 走待办撤回）",
                data={"intent_id": str(doc.intent_id)},
            )

        # 已反做过：幂等返回
        existing = self._find_reversal(tenant_id, doc_id)
        if existing is not None:
            return ReversalOutcome(
                original_doc_id=str(doc_id),
                reversal_doc_id=str(existing.reversal_doc_id),
                idempotent=True,
                detail="该单已撤销过，幂等返回已有反做单（不重复反做）",
            )

        # ① 窗口（老板撤单 / 改单流程传入 ignore_window=True 时跳过硬限制）
        window = int(reversal_spec.get("window_minutes") or DEFAULT_REVERSAL_WINDOW_MINUTES)
        created = doc.created_at
        if created is not None and not ignore_window:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if moment > created + timedelta(minutes=window):
                raise AppError(
                    "E_REVERSAL_WINDOW_CLOSED",
                    message=(
                        f"已超过 {window} 分钟撤销窗口；请用更正流程（sys.correct）修改，"
                        "撤销只能撤刚记的那一笔"
                    ),
                    data={"window_minutes": window, "doc_no": str(doc.doc_no)},
                )

        # ② 被引用
        links = self.references_of(tenant_id=tenant_id, doc_id=doc_id)
        if links and bool(reversal_spec.get("block_if_referenced", True)):
            raise AppError(
                "E_DOC_REFERENCED",
                message="这张单已被其他单据引用，不能直接撤销；请用更正单处理",
                data={
                    "doc_no": str(doc.doc_no),
                    "references": [link.as_dict() for link in links],
                },
            )

        # ③ 反做：数量取反、金额取反，写一张新单
        lines = doc_service.lines_of(tenant_id=tenant_id, doc_id=doc_id)
        if not lines:
            raise AppError("E_VALIDATION", message="单据没有明细行，无法反做")

        draft = DocDraft(
            tenant_id=tenant_id,
            store_id=store_id,
            intent_id=f"{doc.intent_id}",
            biz_date=date.today() if moment is None else moment.date(),
            created_by=member_id,
            payment_status=str(doc.payment_status),
            partner_id=doc.partner_id,
            # 反做单必须自报「我撤的是谁」：报表据此排除、也据此拒绝「撤销的撤销」
            reversal_of=doc_id,
            lines=tuple(
                DocLineDraft(
                    product_id=line.product_id,
                    qty=Decimal(line.qty),
                    unit=str(line.unit),
                    qty_base=Decimal(line.qty_base),
                    unit_price_fen=line.unit_price_fen,
                    amount_fen=line.amount_fen,
                    price_source=line.price_source,
                    cost_snapshot_fen=line.cost_snapshot_fen,
                    cost_status=str(line.cost_status or "final"),
                    batch_id=line.batch_id,
                    source_map=dict(line.source_map or {}),
                )
                for line in lines
            ),
        )
        reversal_doc = doc_service.create(draft, doc_no=f"{doc.doc_no}{REVERSAL_SUFFIX}")
        self._apply_reverse_effects(
            tenant_id=tenant_id,
            store_id=store_id,
            intent_id=str(doc.intent_id),
            original_doc_id=doc_id,
            lines=lines,
            reversal_doc_id=reversal_doc.doc_id,
        )

        doc.status = "reversed"
        doc.reversed_by = reversal_doc.doc_id
        move_reason = reason_of(str(doc.intent_id))
        if move_reason is not None:
            restore_on_reversal(self._session, tenant_id=tenant_id, doc_id=doc_id)
            side_effect = dict(spec.side_effect or {}) if spec is not None else {}
            inv_mode = str(side_effect.get("inventory_delta") or "none")
            reversal_sign = 1 if inv_mode == "decrease" else -1
            record_doc_movements(
                self._session,
                tenant_id=tenant_id,
                store_id=store_id,
                doc_id=reversal_doc.doc_id,
                member_id=member_id,
                reason=move_reason,
                sign=reversal_sign,
                partner_id=doc.partner_id,
            )
        self._session.add(
            Reversal(
                tenant_id=tenant_id,
                store_id=store_id,
                original_doc_id=doc_id,
                reversal_doc_id=reversal_doc.doc_id,
                member_id=member_id,
                reason=reason or "用户撤销",
            )
        )
        self._session.flush()

        self._audit.record(
            tenant_id=tenant_id,
            action="doc.reverse",
            entity="doc_header",
            entity_id=str(doc_id),
            member_id=member_id,
            detail={
                "reversal_doc_id": str(reversal_doc.doc_id),
                "reversal_doc_no": str(reversal_doc.doc_no),
                "reason": reason,
                "window_minutes": window,
                "ignore_window": ignore_window,
            },
        )
        return ReversalOutcome(
            original_doc_id=str(doc_id),
            reversal_doc_id=str(reversal_doc.doc_id),
            reversal_doc_no=str(reversal_doc.doc_no),
            inventory_after=self._inventory_after(tenant_id, store_id, lines),
            detail="已生成反做单，原单保留（两笔都可查、可导出）",
        )

    # -------------------------------------------------------------- 一步原子改单
    def correct_doc(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        doc_id: uuid.UUID,
        member_id: uuid.UUID,
        lines: Sequence[Mapping[str, Any]],
        reason: str = "",
        biz_date: date | None = None,
        payment_status: str | None = None,
        now: datetime | None = None,
    ) -> CorrectionOutcome:
        """一步改单（单事务：反做原单 + 新建 ``corrects_doc_id=doc_id`` 更正单）。"""
        if not lines:
            raise AppError("E_VALIDATION", message="更正后的明细行不能为空")
        doc_service = DocService(self._session)
        orig = doc_service.get(tenant_id=tenant_id, doc_id=doc_id)
        if orig is None:
            raise AppError("E_VALIDATION", message="单据不存在或不属于当前租户")
        orig_lines = doc_service.lines_of(tenant_id=tenant_id, doc_id=doc_id)
        orig_by_pid = {str(line.product_id): line for line in orig_lines}

        # 1) 同事务反做原单（不受 10 分钟窗口限制）
        rev_out = self.reverse(
            tenant_id=tenant_id,
            store_id=store_id,
            doc_id=doc_id,
            member_id=member_id,
            reason=reason or "一步改单：反做原单",
            now=now,
            ignore_window=True,
        )
        if rev_out.reversal_doc_id is None:
            raise AppError("E_VALIDATION", message="反做原单失败")

        # 2) 按新明细构造并落账更正单
        intent_id = str(orig.intent_id)
        spec = self._registry.require(intent_id)
        side_effect = dict(spec.side_effect or {})
        inv_mode = str(side_effect.get("inventory_delta") or "none")
        ar_mode = str(side_effect.get("ar_ap_delta") or "none")
        target_biz_date = biz_date or orig.biz_date
        target_ps = payment_status or str(orig.payment_status)
        policy = (
            batch_service.policy_for(self._session, tenant_id)
            if inv_mode in {"increase", "decrease"}
            else None
        )

        new_line_drafts: list[DocLineDraft] = []
        inv_deltas_by_pid: dict[uuid.UUID, InventoryDelta] = {}
        agg_sale_qty: dict[uuid.UUID, Decimal] = {}

        for raw in lines:
            pid = uuid.UUID(str(raw["product_id"]))
            prev = orig_by_pid.get(str(pid))
            qty = Decimal(str(raw.get("qty", raw.get("quantity", prev.qty if prev else 0))))
            if qty <= 0:
                raise AppError("E_VALIDATION", message="更正后的数量必须大于 0")
            unit = str(raw.get("unit") or (prev.unit if prev else "个"))
            if raw.get("qty_base") is not None:
                qty_base = Decimal(str(raw["qty_base"]))
            elif prev is not None and Decimal(prev.qty) > 0 and unit == str(prev.unit):
                ratio = Decimal(prev.qty_base) / Decimal(prev.qty)
                qty_base = qty * ratio
            else:
                qty_base = qty

            unit_price_fen: int | None = (
                int(raw["unit_price_fen"])
                if raw.get("unit_price_fen") is not None
                else (prev.unit_price_fen if prev is not None else None)
            )
            if raw.get("amount_fen") is not None:
                amount_fen: int | None = int(raw["amount_fen"])
            elif unit_price_fen is not None:
                amount_fen = int(
                    (Decimal(unit_price_fen) * qty).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                )
            else:
                amount_fen = None

            new_line_drafts.append(
                DocLineDraft(
                    product_id=pid,
                    qty=qty,
                    unit=unit,
                    qty_base=qty_base,
                    unit_price_fen=unit_price_fen,
                    amount_fen=amount_fen,
                    price_source="asr",
                    source_map=dict(prev.source_map or {}) if prev else {},
                )
            )
            if inv_mode in {"increase", "decrease"}:
                sign = -1 if inv_mode == "decrease" else 1
                prev_delta = inv_deltas_by_pid.get(pid)
                inv_deltas_by_pid[pid] = InventoryDelta(
                    product_id=pid,
                    qty_base_delta=(prev_delta.qty_base_delta if prev_delta else Decimal(0)) + qty_base * sign,
                )
                if inv_mode == "decrease":
                    agg_sale_qty[pid] = agg_sale_qty.get(pid, Decimal(0)) + qty_base

        sale_plans: dict[uuid.UUID, list[batch_service.BatchAllocation]] = {}
        if inv_mode == "decrease" and policy is not None:
            for pid, t_qty in agg_sale_qty.items():
                sale_plans[pid] = batch_service.plan_sale(
                    self._session,
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=pid,
                    qty_base=t_qty,
                    policy=policy,
                )

        inv_rows_by_pid: dict[uuid.UUID, InventoryRow] = {}
        for pid, delta in inv_deltas_by_pid.items():
            allow_neg = (
                batch_service.effective_allow_negative(self._session, tenant_id, pid, policy)
                if policy is not None
                else True
            )
            for row in InventoryService(self._session).apply(
                tenant_id=tenant_id, store_id=store_id, deltas=[delta], allow_negative=allow_neg
            ):
                inv_rows_by_pid[row.product_id] = row

        new_doc = doc_service.create(
            DocDraft(
                tenant_id=tenant_id,
                store_id=store_id,
                intent_id=intent_id,
                biz_date=target_biz_date,
                created_by=member_id,
                payment_status=target_ps,
                partner_id=orig.partner_id,
                corrects_doc_id=doc_id,
                source=str(orig.source or "manual"),
                lines=tuple(new_line_drafts),
            )
        )

        if ar_mode != "none" and orig.partner_id is not None and target_ps != "已结":
            direction = "receivable" if ar_mode.startswith("receivable") else "payable"
            if int(new_doc.total_amount_fen) > 0:
                ArApService(self._session).apply(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    deltas=[
                        ArApDelta(
                            partner_id=orig.partner_id,
                            direction=direction,
                            amount_fen=int(new_doc.total_amount_fen),
                            doc_id=new_doc.doc_id,
                            settle=False,
                        )
                    ],
                )

        move_reason = reason_of(intent_id)
        created_lines = doc_service.lines_of(tenant_id=tenant_id, doc_id=new_doc.doc_id)
        if move_reason is not None and inv_mode == "increase" and policy is not None:
            for dline in created_lines:
                qb = Decimal(dline.qty_base)
                qty_before = (
                    inv_rows_by_pid[dline.product_id].qty_base_before
                    if dline.product_id in inv_rows_by_pid
                    else Decimal(0)
                )
                uc = (
                    int((Decimal(dline.amount_fen) / qb).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                    if dline.amount_fen is not None and qb > 0
                    else None
                )
                created_batch = None
                if move_reason == "purchase" and batch_service.product_tracks_batch(
                    self._session, tenant_id, dline.product_id, policy
                ):
                    created_batch = batch_service.create_batch(
                        self._session,
                        tenant_id=tenant_id,
                        store_id=store_id,
                        line=dline,
                        partner_id=orig.partner_id,
                        biz_date=target_biz_date,
                    )
                    dline.batch_id = created_batch.batch_id
                batch_service.update_moving_avg_on_purchase(
                    self._session,
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=dline.product_id,
                    qty_before=qty_before,
                    qty_in=qb,
                    unit_cost_fen=uc,
                )
                self._session.add(
                    InventoryMovement(
                        tenant_id=tenant_id,
                        store_id=store_id,
                        product_id=dline.product_id,
                        batch_id=created_batch.batch_id if created_batch else None,
                        doc_id=new_doc.doc_id,
                        doc_line_id=dline.line_id,
                        reason=move_reason,
                        qty_base=qb,
                        unit_cost_fen=uc,
                        cost_status="final" if uc is not None else "unknown",
                        partner_id=orig.partner_id,
                        created_by=member_id,
                    )
                )
            self._session.flush()
        elif move_reason is not None and inv_mode == "decrease":
            all_allocs: list[batch_service.BatchAllocation] = []
            lines_by_pid: dict[uuid.UUID, DocLine] = {ln.product_id: ln for ln in created_lines}
            for pid, allocs in sale_plans.items():
                batch_service.apply_sale(self._session, allocs)
                all_allocs.extend(allocs)
                dline = lines_by_pid.get(pid)
                for item in allocs:
                    self._session.add(
                        InventoryMovement(
                            tenant_id=tenant_id,
                            store_id=store_id,
                            product_id=pid,
                            batch_id=item.batch_id,
                            doc_id=new_doc.doc_id,
                            doc_line_id=dline.line_id if dline else None,
                            reason=move_reason,
                            qty_base=-item.qty_base,
                            unit_cost_fen=item.unit_cost_fen,
                            cost_status=item.cost_status,
                            partner_id=orig.partner_id,
                            created_by=member_id,
                        )
                    )
                if dline is not None:
                    dline.cost_snapshot_fen, dline.cost_status = batch_service.cogs_detail_of(allocs)
            new_doc.cogs_fen = batch_service.cogs_of(all_allocs)
            self._session.flush()

        self._audit.record(
            tenant_id=tenant_id,
            action="doc.correct",
            entity="doc_header",
            entity_id=str(doc_id),
            member_id=member_id,
            detail={
                "reversal_doc_id": str(rev_out.reversal_doc_id),
                "new_doc_id": str(new_doc.doc_id),
                "new_doc_no": str(new_doc.doc_no),
                "reason": reason,
            },
        )
        return CorrectionOutcome(
            original_doc_id=str(doc_id),
            reversal_doc_id=str(rev_out.reversal_doc_id),
            new_doc_id=str(new_doc.doc_id),
            new_doc_no=str(new_doc.doc_no),
            total_amount_fen=int(new_doc.total_amount_fen),
            inventory_after=self._inventory_after(tenant_id, store_id, created_lines),
        )

    # ------------------------------------------------------------------ 内部
    def _find_reversal(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> Reversal | None:
        """查已有反做记录。"""
        statement = (
            select(Reversal)
            .where(Reversal.tenant_id == tenant_id, Reversal.original_doc_id == doc_id)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _apply_reverse_effects(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        intent_id: str,
        original_doc_id: uuid.UUID,
        lines: list[DocLine],
        reversal_doc_id: uuid.UUID,
    ) -> None:
        """把原单的库存/应收效果**反向**应用一次。"""
        del reversal_doc_id
        spec = self._registry.get(intent_id)
        side_effect = dict(spec.side_effect or {}) if spec is not None else {}
        inventory_mode = str(side_effect.get("inventory_delta") or "none")
        ar_mode = str(side_effect.get("ar_ap_delta") or "none")

        if inventory_mode in {"increase", "decrease"}:
            sign = 1 if inventory_mode == "decrease" else -1
            deltas_by_pid: dict[uuid.UUID, InventoryDelta] = {}
            for line in lines:
                prev = deltas_by_pid.get(line.product_id)
                delta_qty = Decimal(line.qty_base) * sign
                deltas_by_pid[line.product_id] = InventoryDelta(
                    product_id=line.product_id,
                    qty_base_delta=(prev.qty_base_delta if prev else Decimal(0)) + delta_qty,
                )
            InventoryService(self._session).apply(
                tenant_id=tenant_id,
                store_id=store_id,
                deltas=list(deltas_by_pid.values()),
                allow_negative=True,
            )
        elif inventory_mode == "snapshot":
            raise AppError(
                "E_VALIDATION",
                message="盘点快照没有留档前值，不能反做；请重新盘一次覆盖当前数量",
                data={"reason": "snapshot_not_reversible"},
            )

        if ar_mode != "none":
            ArApService(self._session).reverse_doc_ar_ap(
                tenant_id=tenant_id, doc_id=original_doc_id
            )

    def _partner_of(self, tenant_id: uuid.UUID, line: DocLine) -> uuid.UUID | None:
        """从原单对应的应收应付反查往来单位。"""
        row = self._session.execute(
            select(ArAp)
            .where(ArAp.tenant_id == tenant_id, ArAp.doc_id == line.doc_id)
            .limit(1)
        ).scalar_one_or_none()
        return None if row is None else row.partner_id

    def _inventory_after(
        self, tenant_id: uuid.UUID, store_id: uuid.UUID, lines: list[DocLine]
    ) -> dict[str, str]:
        """反做后的库存快照。"""
        service = InventoryService(self._session)
        result: dict[str, str] = {}
        for line in lines:
            row = service.get_qty_base(
                tenant_id=tenant_id, store_id=store_id, product_id=line.product_id
            )
            if row is not None:
                result[str(line.product_id)] = str(row.qty_base)
        return result
