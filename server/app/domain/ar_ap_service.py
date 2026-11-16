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

from app.models.doc import ArAp

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
        """单条改动：有未结记录就核销/累加，没有就新建。"""
        if delta.direction not in {DIRECTION_RECEIVABLE, DIRECTION_PAYABLE}:
            raise ValueError(f"未知的应收应付方向 {delta.direction!r}")
        key = f"{delta.partner_id}:{delta.direction}"
        row = self.get(tenant_id=tenant_id, partner_id=delta.partner_id, direction=delta.direction)

        if row is None:
            return self._create(tenant_id, store_id, delta, key)

        before_amount = int(row.amount_fen)
        before_settled = int(row.settled_amount_fen)
        outstanding = before_amount - before_settled

        if delta.settle:
            if delta.amount_fen > outstanding:
                raise OverSettleBlocked(
                    key=key, outstanding_fen=outstanding, delta_fen=delta.amount_fen
                )
            after_settled = before_settled + delta.amount_fen
            after_amount = before_amount
        else:
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
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
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
