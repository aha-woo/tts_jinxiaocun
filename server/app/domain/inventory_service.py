"""库存领域服务：**门店级** ``qty_base`` 的 CAS 写入与读取。

两个不变式（架构 §2.3 ⑥ 并发保证）
--------------------------------
1. **只用 CAS 写**：``UPDATE ... WHERE version = :expected``，影响行数 0 就是版本冲突
   （别人先落了账），抛 :class:`VersionConflict`，由上层翻译成
   ``E_PREVIEW_VERSION_CONFLICT``(1202)。**绝不静默覆盖**：静默覆盖等于把并发录的两笔账
   合并成一笔，用户还看不出来。
2. **数量用 Decimal**：``qty_base`` 是 ``Numeric(18,4)``，任何环节的 ``float`` 都会
   在累计后变成 0.30000000000000004 这类脏值，再折算成箱就错了。

本模块**不 commit**：所有写操作都在调用方的事务里，保证
「库存 + 单据 + 应收 + 审计」要么全成、要么全滚。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.inventory import Inventory

__all__ = [
    "InventoryDelta",
    "InventoryRow",
    "InventoryService",
    "NegativeStockBlocked",
    "VersionConflict",
]


class VersionConflict(RuntimeError):
    """CAS 版本冲突（预演时的 version 已经不是最新的）。"""

    def __init__(self, *, product_id: Any, expected: int, actual: int | None = None) -> None:
        self.product_id = product_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"库存版本冲突：product_id={product_id} 期望 version={expected}，"
            f"实际={actual}（并发录单，必须重新只读预演）"
        )


class NegativeStockBlocked(RuntimeError):
    """落账会把库存写成负数（``inventory.qty_base >= 0`` 是 DB 级 CHECK）。"""

    def __init__(self, *, product_id: Any, after: Decimal) -> None:
        self.product_id = product_id
        self.after = after
        super().__init__(
            f"落账会让 product_id={product_id} 的库存变负（{after}），"
            "请先入库或改数量（DB CHECK ck_inventory_qty_nonneg 会直接拒）"
        )


@dataclass(frozen=True)
class InventoryDelta:
    """一条库存改动（已折算到 base_unit）。"""

    product_id: uuid.UUID
    qty_base_delta: Decimal
    #: 预演时的 version（CAS 期望值）；``None`` 表示「新行，按不存在处理」
    expected_version: int | None = None
    snapshot: bool = False


@dataclass(frozen=True)
class InventoryRow:
    """写入后的库存行快照（回执与审计用）。"""

    product_id: uuid.UUID
    qty_base_before: Decimal
    qty_base_after: Decimal
    version_before: int
    version_after: int
    created: bool = False


class InventoryService:
    """库存读写（写走 CAS，读走普通 SELECT）。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（RLS 生效）。
        """
        self._session = session

    # ------------------------------------------------------------------ 读
    def get_qty_base(
        self, *, tenant_id: uuid.UUID, store_id: uuid.UUID, product_id: uuid.UUID
    ) -> Inventory | None:
        """取库存行（不存在返回 ``None``，**不新建**）。"""
        statement = (
            select(Inventory)
            .where(
                Inventory.tenant_id == tenant_id,
                Inventory.store_id == store_id,
                Inventory.product_id == product_id,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_products(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        product_ids: Sequence[uuid.UUID],
    ) -> list[Inventory]:
        """批量取库存行（只读查询用）。"""
        if not product_ids:
            return []
        statement = select(Inventory).where(
            Inventory.tenant_id == tenant_id,
            Inventory.store_id == store_id,
            Inventory.product_id.in_(list(product_ids)),
        )
        return list(self._session.execute(statement).scalars())

    # ------------------------------------------------------------------ 写
    def apply(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        deltas: Sequence[InventoryDelta],
        allow_negative: bool = True,
    ) -> tuple[InventoryRow, ...]:
        """按 CAS 应用一批库存改动（**同一事务**，只 flush 不 commit）。

        Args:
            tenant_id: 租户。
            store_id: 门店。
            deltas: 改动行。
            allow_negative: 是否允许库存为负（默认允许，禁止时抛 NegativeStockBlocked）。

        Returns:
            每行改动后的快照。

        Raises:
            VersionConflict: 某行的 version 已不是预演时的值。
            NegativeStockBlocked: 禁止负库存且改动后为负。
        """
        results: list[InventoryRow] = []
        for delta in deltas:
            row = self.get_qty_base(
                tenant_id=tenant_id, store_id=store_id, product_id=delta.product_id
            )
            if row is None and delta.expected_version not in (None, 0):
                raise VersionConflict(
                    product_id=delta.product_id, expected=int(delta.expected_version), actual=None
                )
            if row is None:
                results.append(self._create(tenant_id, store_id, delta, allow_negative=allow_negative))
                continue
            results.append(self._update(row, delta, allow_negative=allow_negative))
        return tuple(results)

    def _create(
        self,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        delta: InventoryDelta,
        *,
        allow_negative: bool = True,
    ) -> InventoryRow:
        """新建库存行（首次进货 / 首次负库存卖出 / 首次盘点）。"""
        after = delta.qty_base_delta
        if after < 0 and not allow_negative:
            raise NegativeStockBlocked(product_id=delta.product_id, after=after)
        row = Inventory(
            tenant_id=tenant_id,
            store_id=store_id,
            product_id=delta.product_id,
            qty_base=after,
            negative_flag=(after < 0),
            version=0,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            # 并发下别人刚插入同一 (tenant, store, product) → 视为版本冲突（SAVEPOINT 不破坏外层事务）
            raise VersionConflict(
                product_id=delta.product_id, expected=0, actual=None
            ) from exc
        return InventoryRow(
            product_id=delta.product_id,
            qty_base_before=Decimal("0"),
            qty_base_after=after,
            version_before=0,
            version_after=0,
            created=True,
        )

    def _update(
        self, row: Inventory, delta: InventoryDelta, *, allow_negative: bool = True
    ) -> InventoryRow:
        """CAS 更新库存行。"""
        before = Decimal(row.qty_base)
        after = delta.qty_base_delta if delta.snapshot else before + delta.qty_base_delta
        if after < 0 and not allow_negative:
            raise NegativeStockBlocked(product_id=delta.product_id, after=after)
        expected = int(row.version) if delta.expected_version is None else int(delta.expected_version)
        statement = (
            update(Inventory)
            .where(
                Inventory.inventory_id == row.inventory_id,
                Inventory.tenant_id == row.tenant_id,
                Inventory.version == expected,
            )
            .values(qty_base=after, negative_flag=(after < 0), version=expected + 1)
            .execution_options(synchronize_session=False)
        )
        outcome = self._session.execute(statement)
        if outcome.rowcount != 1:
            current = self.get_qty_base(
                tenant_id=row.tenant_id,
                store_id=row.store_id,
                product_id=row.product_id,
            )
            raise VersionConflict(
                product_id=delta.product_id,
                expected=expected,
                actual=int(current.version) if current is not None else None,
            )
        self._session.flush()
        self._session.expire(row)
        return InventoryRow(
            product_id=delta.product_id,
            qty_base_before=before,
            qty_base_after=after,
            version_before=expected,
            version_after=expected + 1,
        )
