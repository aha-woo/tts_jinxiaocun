"""应收应付领域服务：**欠款按租户汇总、门店分摊**（PRD 2.8），核销走 CAS。

为什么欠款要 CAS
----------------
「收了一笔款」= 把 ``settled_amount_fen`` 加上去。两个人同时收同一笔账
（老板 + 店员各记一次），不做 CAS 就会**重复核销**：账面显示已结清，
实际只收到一半的钱。这类错不会报错、也不会有人投诉，只会安静地少一笔账。

因此：

- 每次核销校验 ``version``，不匹配抛 :class:`VersionConflict`（上层翻 1202）；
- ``settled_amount_fen <= amount_fen`` 由 DB ``CHECK ck_ar_ap_amounts`` 兜底，
  本模块在写之前先给出人话原因（超额核销 = 记错账）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.doc import ArAp, Payment

__all__ = [
    "ArApDelta",
    "ArApRow",
    "ArApService",
    "OverSettleBlocked",
    "VersionConflict",
]

#: 方向（与 DB CHECK 一致）
DIRECTION_RECEIVABLE = "receivable"
DIRECTION_PAYABLE = "payable"


class VersionConflict(RuntimeError):
    """应收应付核销的 CAS 冲突。"""

    def __init__(self, *, key: str, expected: int, actual: int | None = None) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(f"应收应付版本冲突：{key} 期望 version={expected}，实际={actual}")


class OverSettleBlocked(RuntimeError):
    """核销金额超过未结金额（= 记错账，DB CHECK 也会拒）。"""

    def __init__(self, *, key: str, outstanding_fen: int, delta_fen: int) -> None:
        self.key = key
        self.outstanding_fen = outstanding_fen
        self.delta_fen = delta_fen
        super().__init__(
            f"{key} 未结 {outstanding_fen} 分，这次核销 {delta_fen} 分：超额核销，拒"
        )


@dataclass(frozen=True)
class ArApDelta:
    """一条应收应付改动。"""

    partner_id: uuid.UUID
    direction: str
    amount_fen: int
    doc_id: uuid.UUID | None = None
    expected_version: int | None = None
    #: ``True`` 时 amount_fen 是**合计**（盘点式覆盖），否则是增量
    snapshot: bool = False
    #: 核销（把 settled 加上去）还是新增欠款（把 amount 加上去）
    settle: bool = False


@dataclass(frozen=True)
class ArApRow:
    """写入后的应收应付快照。"""

    ar_ap_id: uuid.UUID
    key: str
    amount_fen: int
    settled_amount_fen: int
    outstanding_fen: int
    status: str
    version_before: int
    version_after: int
    allocations: tuple[tuple[uuid.UUID, int], ...] = ()


class ArApService:
    """应收应付读写。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
        """
        self._session = session

    # ------------------------------------------------------------------ 读
    def get(
        self,
        *,
        tenant_id: uuid.UUID,
        partner_id: uuid.UUID,
        direction: str,
    ) -> ArAp | None:
        """按 (partner, direction) 取**最近一条**未结记录。"""
        statement = (
            select(ArAp)
            .where(
                ArAp.tenant_id == tenant_id,
                ArAp.partner_id == partner_id,
                ArAp.direction == direction,
                ArAp.status != "settled",
            )
            .order_by(ArAp.created_at.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def outstanding_of(
        self, *, tenant_id: uuid.UUID, partner_id: uuid.UUID, direction: str
    ) -> int:
        """未结金额（分）。"""
        rows = self.list_open(tenant_id=tenant_id, partner_id=partner_id, direction=direction)
        return sum(int(row.amount_fen) - int(row.settled_amount_fen) for row in rows)

    def list_open(
        self,
        *,
        tenant_id: uuid.UUID,
        partner_id: uuid.UUID | None = None,
        direction: str | None = None,
    ) -> list[ArAp]:
        """列出未结清记录（欠款查询用；``partner_id=None`` 表示全部）。"""
        statement = select(ArAp).where(
            ArAp.tenant_id == tenant_id, ArAp.status != "settled"
        )
        if partner_id is not None:
            statement = statement.where(ArAp.partner_id == partner_id)
        if direction is not None:
            statement = statement.where(ArAp.direction == direction)
        statement = statement.order_by(ArAp.created_at.desc())
        return list(self._session.execute(statement).scalars())

    # ------------------------------------------------------------------ 写
    def apply(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        deltas: Sequence[ArApDelta],
    ) -> tuple[ArApRow, ...]:
        """应用一批应收应付改动（CAS，只 flush 不 commit）。"""
        rows: list[ArApRow] = []
        for delta in deltas:
            rows.append(self._apply_one(tenant_id, store_id, delta))
        return tuple(rows)

    def _apply_one(
        self, tenant_id: uuid.UUID, store_id: uuid.UUID, delta: ArApDelta
    ) -> ArApRow:
        """单条改动：按单据独立记应收应付；核销时按最早未结单据 FIFO 依次核销。"""
        if delta.direction not in {DIRECTION_RECEIVABLE, DIRECTION_PAYABLE}:
            raise ValueError(f"未知的应收应付方向 {delta.direction!r}")
        key = f"{delta.partner_id}:{delta.direction}"

        # 若带了真 doc_id 且不是核销（赊账新单），必须按 (tenant_id, doc_id, direction) 建独立行，
        # 否则多笔赊账混成同一条记录会导致撤单引用链出错。
        if not delta.settle and delta.doc_id is not None:
            existing_for_doc = self._session.execute(
                select(ArAp)
                .where(
                    ArAp.tenant_id == tenant_id,
                    ArAp.doc_id == delta.doc_id,
                    ArAp.direction == delta.direction,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing_for_doc is None:
                return self._create(tenant_id, store_id, delta, key)

        if delta.settle:
            open_rows = list(
                self._session.execute(
                    select(ArAp)
                    .where(
                        ArAp.tenant_id == tenant_id,
                        ArAp.partner_id == delta.partner_id,
                        ArAp.direction == delta.direction,
                        ArAp.status != "settled",
                    )
                    .order_by(ArAp.created_at.asc())
                ).scalars()
            )
            if not open_rows:
                raise OverSettleBlocked(key=key, outstanding_fen=0, delta_fen=delta.amount_fen)
            total_outstanding = sum(int(r.amount_fen) - int(r.settled_amount_fen) for r in open_rows)
            if delta.amount_fen > total_outstanding:
                raise OverSettleBlocked(
                    key=key, outstanding_fen=total_outstanding, delta_fen=delta.amount_fen
                )
            remaining = int(delta.amount_fen)
            last_result: ArApRow | None = None
            allocated: list[tuple[uuid.UUID, int]] = []
            for idx, r in enumerate(open_rows):
                if remaining <= 0:
                    break
                row_out = int(r.amount_fen) - int(r.settled_amount_fen)
                if row_out <= 0:
                    continue
                pay_here = min(row_out, remaining)
                expected = (
                    int(delta.expected_version)
                    if (idx == 0 and delta.expected_version is not None)
                    else int(r.version)
                )
                before_amount = int(r.amount_fen)
                after_settled = int(r.settled_amount_fen) + pay_here
                status = _status_of(before_amount, after_settled)
                outcome = self._session.execute(
                    update(ArAp)
                    .where(
                        ArAp.ar_ap_id == r.ar_ap_id,
                        ArAp.tenant_id == tenant_id,
                        ArAp.version == expected,
                    )
                    .values(
                        settled_amount_fen=after_settled,
                        status=status,
                        version=expected + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if outcome.rowcount != 1:
                    current = self.get(
                        tenant_id=tenant_id, partner_id=delta.partner_id, direction=delta.direction
                    )
                    raise VersionConflict(
                        key=key, expected=expected, actual=int(current.version) if current else None
                    )
                self._session.flush()
                self._session.expire(r)
                remaining -= pay_here
                allocated.append((r.ar_ap_id, pay_here))
                last_result = ArApRow(
                    ar_ap_id=r.ar_ap_id,
                    key=key,
                    amount_fen=before_amount,
                    settled_amount_fen=after_settled,
                    outstanding_fen=before_amount - after_settled,
                    status=status,
                    version_before=expected,
                    version_after=expected + 1,
                )
            assert last_result is not None
            return ArApRow(
                ar_ap_id=last_result.ar_ap_id,
                key=last_result.key,
                amount_fen=last_result.amount_fen,
                settled_amount_fen=last_result.settled_amount_fen,
                outstanding_fen=last_result.outstanding_fen,
                status=last_result.status,
                version_before=last_result.version_before,
                version_after=last_result.version_after,
                allocations=tuple(allocated),
            )

        row = self.get(tenant_id=tenant_id, partner_id=delta.partner_id, direction=delta.direction)
        if row is None:
            return self._create(tenant_id, store_id, delta, key)

        before_amount = int(row.amount_fen)
        before_settled = int(row.settled_amount_fen)
        after_settled = before_settled
        after_amount = (
            delta.amount_fen if delta.snapshot else before_amount + delta.amount_fen
        )
        status = _status_of(after_amount, after_settled)
        expected = int(row.version) if delta.expected_version is None else int(delta.expected_version)
        statement = (
            update(ArAp)
            .where(
                ArAp.ar_ap_id == row.ar_ap_id,
                ArAp.tenant_id == tenant_id,
                ArAp.version == expected,
            )
            .values(
                amount_fen=after_amount,
                settled_amount_fen=after_settled,
                status=status,
                version=expected + 1,
            )
            .execution_options(synchronize_session=False)
        )
        outcome = self._session.execute(statement)
        if outcome.rowcount != 1:
            current = self.get(
                tenant_id=tenant_id, partner_id=delta.partner_id, direction=delta.direction
            )
            raise VersionConflict(
                key=key, expected=expected, actual=int(current.version) if current else None
            )
        self._session.flush()
        self._session.expire(row)
        return ArApRow(
            ar_ap_id=row.ar_ap_id,
            key=key,
            amount_fen=after_amount,
            settled_amount_fen=after_settled,
            outstanding_fen=after_amount - after_settled,
            status=status,
            version_before=expected,
            version_after=expected + 1,
        )

    def reverse_doc_ar_ap(self, *, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> int:
        """撤单时冲销原单关联的未核销应收应付记录。"""
        rows = list(
            self._session.execute(
                select(ArAp).where(ArAp.tenant_id == tenant_id, ArAp.doc_id == doc_id)
            ).scalars()
        )
        for row in rows:
            if int(row.settled_amount_fen) == 0:
                row.amount_fen = 0
                row.settled_amount_fen = 0
                row.status = "settled"
                row.version = int(row.version) + 1
        if rows:
            self._session.flush()
        return len(rows)

    def reverse_payment_doc(
        self, *, tenant_id: uuid.UUID, payment_doc_id: uuid.UUID
    ) -> int:
        """撤销收/付款核销单时回退关联 ``ArAp.settled_amount_fen`` 并冲销对应付款流水。"""
        prefix = f"doc:{payment_doc_id}"
        payments = list(
            self._session.execute(
                select(Payment).where(
                    Payment.tenant_id == tenant_id,
                    (Payment.idempotency_key == prefix)
                    | Payment.idempotency_key.like(f"{prefix}:%"),
                )
            ).scalars()
        )
        for pay in payments:
            ar_ap = self._session.execute(
                select(ArAp)
                .where(ArAp.tenant_id == tenant_id, ArAp.ar_ap_id == pay.ar_ap_id)
                .limit(1)
            ).scalar_one_or_none()
            if ar_ap is not None:
                after_settled = max(0, int(ar_ap.settled_amount_fen) - int(pay.amount_fen))
                ar_ap.settled_amount_fen = after_settled
                ar_ap.status = _status_of(int(ar_ap.amount_fen), after_settled)
                ar_ap.version = int(ar_ap.version) + 1
            self._session.delete(pay)
        if payments:
            self._session.flush()
        return len(payments)

    def record_payment(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        ar_ap_id: uuid.UUID,
        amount_fen: int,
        biz_date: Any,
        idempotency_key: str,
        method: str = "现金",
        note: str | None = None,
    ) -> Payment:
        """落库收付款流水（与核销同一事务）。"""
        existing = self._session.execute(
            select(Payment)
            .where(Payment.tenant_id == tenant_id, Payment.idempotency_key == idempotency_key)
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        payment = Payment(
            tenant_id=tenant_id,
            store_id=store_id,
            ar_ap_id=ar_ap_id,
            amount_fen=amount_fen,
            method=method,
            biz_date=biz_date,
            idempotency_key=idempotency_key,
            note=note,
        )
        with self._session.begin_nested():
            self._session.add(payment)
            self._session.flush()
        return payment

    def _create(
        self, tenant_id: uuid.UUID, store_id: uuid.UUID, delta: ArApDelta, key: str
    ) -> ArApRow:
        """新建一条欠款（首笔未结）。"""
        amount = delta.amount_fen
        settled = 0
        if delta.settle:
            # 没有未结记录却要核销：只可能是账已结清，直接拒（记错账的前兆）
            raise OverSettleBlocked(key=key, outstanding_fen=0, delta_fen=delta.amount_fen)
        row = ArAp(
            tenant_id=tenant_id,
            store_id=store_id,
            partner_id=delta.partner_id,
            doc_id=delta.doc_id or uuid.uuid4(),
            direction=delta.direction,
            amount_fen=amount,
            settled_amount_fen=settled,
            status=_status_of(amount, settled),
            version=0,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise VersionConflict(key=key, expected=0, actual=None) from exc
        return ArApRow(
            ar_ap_id=row.ar_ap_id,
            key=key,
            amount_fen=amount,
            settled_amount_fen=settled,
            outstanding_fen=amount,
            status=_status_of(amount, settled),
            version_before=0,
            version_after=0,
        )


def _status_of(amount_fen: int, settled_fen: int) -> str:
    """按金额推状态（``open`` / ``partial`` / ``settled``）。"""
    if settled_fen <= 0:
        return "open"
    if settled_fen >= amount_fen:
        return "settled"
    return "partial"


def to_decimal(value: Any) -> Decimal:
    """数量/金额的安全 Decimal 转换（禁止 float 直通）。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))
