"""落账必须 CAS，而且冲突**真能触发**（验收④，本轮硬门槛第二条）。

要证明的两件事
--------------
1. **CAS 是真 CAS**：``UPDATE ... WHERE version = :expected``，影响 0 行就报冲突。
   不是「先读到 v1、再写 v1」那种假乐观锁（读和写之间有窗口，一样会覆盖）；
2. **冲突真的会发生**：预演拿到 v1 → 另一个事务把它改成 v2 → 落账必须失败返 1202，
   **绝不静默覆盖**（静默覆盖 = 把并发录的两笔账合成一笔，用户还看不出来）。

因此下面的测试刻意用两个独立会话构造「预演之后被人抢先落账」的真实时序，
而不是直接构造一个坏 version 去骗断言。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.common.errors import AppError
from app.domain.inventory_service import InventoryDelta, InventoryService, VersionConflict
from app.models.doc import DocHeader
from app.models.inventory import Inventory
from app.models.quota import UsageLedger
from app.models.voice import ConfirmCard
from app.services.ledger import LedgerService
from t04_fixtures import count_rows, create_card_for, lines_for, seed  # noqa: F401  pytest 夹具


def _inventory_of(seed_obj) -> Inventory:
    """读主仓那条库存行（绕过 scope，直接看库里真实的数）。"""
    with seed_obj.database.session(bypass_scope=True) as session:
        return session.execute(
            select(Inventory).where(
                Inventory.tenant_id == seed_obj.tenant_id,
                Inventory.store_id == seed_obj.store_id,
                Inventory.product_id == seed_obj.product_id,
            )
        ).scalar_one()


def test_real_concurrent_conflict_returns_1202_and_never_overwrites(seed) -> None:
    """预演 v1 → 别人改成 v2 → 落账失败返 1202，且库存保持别人的新值（不被覆盖）。"""
    database = seed.database
    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session)
        card_id = uuid.UUID(creation.card_id)
        # 卡片上钉住的就是预演时的版本（CAS 的期望值来源）
        assert creation.preview_version == 0

    # ---- 另一个事务抢先落账（模拟并发录单）：v0 → v1，库存 192 → 204
    with database.session(context=seed.context) as session:
        InventoryService(session).apply(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            deltas=[
                InventoryDelta(
                    product_id=seed.product_id,
                    qty_base_delta=Decimal("12"),
                    expected_version=0,
                )
            ],
        )

    # ---- 这张卡再确认：必须失败返 1202
    with pytest.raises(AppError) as excinfo:
        with database.session(context=seed.context) as session:
            LedgerService(session).confirm(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                member_id=seed.member_id,
                card_id=card_id,
                confirm_type="button",
                button_fallback=True,
            )
    assert excinfo.value.resolved_code == "E_PREVIEW_VERSION_CONFLICT"
    assert excinfo.value.spec().numeric == 1202
    assert "重新" in excinfo.value.resolved_message, "提示必须引导重新只读预演，不能只说失败"

    # ---- 不许静默覆盖：库存是 204（192+12），不是 240（把别人那笔抹掉）
    row = _inventory_of(seed)
    assert Decimal(row.qty_base) == Decimal("204"), "落账静默覆盖了并发写入的库存"
    assert int(row.version) == 1

    # ---- 也不许留下半张单 / 半次扣次
    assert count_rows(database, DocHeader) == 0, "冲突了却写出了单据"
    assert count_rows(database, UsageLedger) == 1, "冲突不该额外扣次（建卡那次已有 1 条）"
    assert count_rows(database, ConfirmCard) == 1
    with database.session(bypass_scope=True) as session:
        card = session.execute(select(ConfirmCard).where(ConfirmCard.card_id == card_id)).scalar_one()
        assert str(card.status) != "confirmed", "冲突了却把卡片标成已确认"


def test_inventory_cas_is_a_real_conditional_update(seed) -> None:
    """同一个 ``expected_version`` 只能成功一次：第二次影响 0 行 → ``VersionConflict``。

    这条直接钉住 SQL 形态（``WHERE version = :expected``）：
    如果实现是「先 SELECT 再赋值」，第二次写会**成功**并把版本覆盖回去，测试就会红。
    """
    with seed.database.session(context=seed.context) as session:
        service = InventoryService(session)
        first = service.apply(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            deltas=[
                InventoryDelta(
                    product_id=seed.product_id, qty_base_delta=Decimal("48"), expected_version=0
                )
            ],
        )
        assert first[0].version_before == 0 and first[0].version_after == 1
        assert first[0].qty_base_after == Decimal("240")

        with pytest.raises(VersionConflict) as excinfo:
            service.apply(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                deltas=[
                    InventoryDelta(
                        product_id=seed.product_id,
                        qty_base_delta=Decimal("48"),
                        expected_version=0,  # 用过期的期望值
                    )
                ],
            )
        assert excinfo.value.expected == 0 and excinfo.value.actual == 1

    row = _inventory_of(seed)
    assert Decimal(row.qty_base) == Decimal("240"), "过期的期望值居然写成功了"


def test_negative_stock_is_blocked_instead_of_silently_clamped(seed) -> None:
    """开启「禁止负库存」时，落账后为负 → 明确拒绝（不是「算出来是负数就当 0」）。"""
    from app.models.tenant import Tenant

    database = seed.database
    with database.session(bypass_scope=True) as session:
        tenant = session.get(Tenant, seed.tenant_id)
        assert tenant is not None
        tenant.allow_negative = False

    with database.session(context=seed.context) as session:
        _, creation = create_card_for(
            seed,
            session=session,
            intent_id="sales.create",
            slots={"P": "可乐", "Q": 9999, "U": "箱"},
            lines=lines_for(seed, quantity=9999, qty_base=9999 * 24, unit_price_fen=3500),
        )
        card_id = uuid.UUID(creation.card_id)

    with pytest.raises(AppError) as excinfo:
        with database.session(context=seed.context) as session:
            # sales.create 是 C1，不需要二次语音
            LedgerService(session).confirm(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                member_id=seed.member_id,
                card_id=card_id,
                confirm_type="button",
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert excinfo.value.data.get("reason") == "negative_stock"
    assert Decimal(_inventory_of(seed).qty_base) == seed.qty_base, "库存被写成负数或 0"
    assert count_rows(database, DocHeader) == 0
