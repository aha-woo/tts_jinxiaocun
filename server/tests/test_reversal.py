"""撤销 = **反做单**：原单保留不删、生成反向单据、留关联、超窗拒、被引用拒（验收⑤ 4.4）。

为什么这些断言值得写
------------------
「删掉一笔单」看起来最省事，但账面会平白少一笔、事后谁也说不清发生过什么。
反做单是**再记一笔反向的**：两笔都在、都能查、都能导出。所以本文件断言的重点不是
「能不能撤」，而是：

- 原单**必须还在**且状态为 ``reversed``（不是被删掉）；
- 反做单的单号是 ``原单号 + "-R"``（一眼看出是反向单据）；
- 库存/应收被**反向再记一次**，账才算平；
- 超过窗口 → ``E_REVERSAL_WINDOW_CLOSED``(1203)；被引用 → ``E_DOC_REFERENCED``(1204) 且**展示引用链**；
- R0 动作**不存在撤销**（没写过，撤什么）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.common.errors import AppError
from app.domain.doc_service import DocDraft, DocService
from app.domain.intent_registry import default_registry
from app.models.dict import Reversal
from app.models.doc import ArAp, DocHeader
from app.models.inventory import Inventory
from app.models.ops import AuditLog
from app.services.ledger import LedgerService
from app.services.reversal import ReversalService
from t04_fixtures import count_rows, create_card_for, seed  # noqa: F401  pytest 夹具


def _confirm(seed_obj, *, intent_id: str = "purchase.create", c2: bool = False):
    """建卡并确认，返回 ``(doc_id, t0)``。"""
    t0 = datetime.now(timezone.utc)
    with seed_obj.database.session(context=seed_obj.context) as session:
        _, creation = create_card_for(seed_obj, session=session, intent_id=intent_id, now=t0)
        card_id = creation.card_id
    with seed_obj.database.session(context=seed_obj.context) as session:
        outcome = LedgerService(session).confirm(
            tenant_id=seed_obj.tenant_id,
            store_id=seed_obj.store_id,
            member_id=seed_obj.member_id,
            card_id=uuid.UUID(card_id),
            confirm_type="voice2" if c2 else "button",
            spoken_text="确认" if c2 else None,
            button_fallback=True,
            now=t0 + timedelta(seconds=2),
        )
    assert outcome.confirmed is True, outcome.as_dict()
    return uuid.UUID(str(outcome.doc_id)), t0


def _doc(seed_obj, doc_id: uuid.UUID) -> DocHeader:
    """读单据实体（绕过 scope）。"""
    with seed_obj.database.session(bypass_scope=True) as session:
        return session.execute(select(DocHeader).where(DocHeader.doc_id == doc_id)).scalar_one()


def test_reversal_creates_a_counter_doc_and_keeps_the_original(seed) -> None:
    """反做单：原单保留 + 反向单据 + 关联记录 + 库存反向记账。"""
    doc_id, t0 = _confirm(seed)
    original = _doc(seed, doc_id)
    assert str(original.status) == "confirmed"

    with seed.database.session(context=seed.context) as session:
        outcome = ReversalService(session).reverse(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            doc_id=doc_id,
            member_id=seed.member_id,
            reason="说错了",
            now=t0 + timedelta(minutes=1),
        )

    assert outcome.reversal_doc_id is not None
    assert outcome.reversal_doc_no == f"{original.doc_no}-R", "反做单号不是原单号 + -R"

    # 原单还在，只是状态变了（**绝不物理删除**）
    refreshed = _doc(seed, doc_id)
    assert refreshed is not None, "原单被删掉了"
    assert str(refreshed.status) == "reversed"
    assert str(refreshed.reversed_by) == outcome.reversal_doc_id

    # 反做单本身是一张新单，金额相同
    reversal_doc = _doc(seed, uuid.UUID(outcome.reversal_doc_id))
    assert int(reversal_doc.total_amount_fen) == int(original.total_amount_fen)

    # 关联记录 + 审计
    assert count_rows(seed.database, Reversal) == 1
    with seed.database.session(bypass_scope=True) as session:
        actions = [row.action for row in session.execute(select(AuditLog)).scalars()]
    assert "doc.reverse" in actions

    # 库存被反向再记一次：192 + 48（进货）→ 240，撤销后回到 192
    with seed.database.session(bypass_scope=True) as session:
        row = session.execute(select(Inventory)).scalar_one()
        assert Decimal(row.qty_base) == seed.qty_base, "撤销没有把库存反向加回去"

    # 两笔单都在（这才叫「可查、可导出」）
    assert count_rows(seed.database, DocHeader) == 2


def test_reversal_is_idempotent(seed) -> None:
    """同一张单撤两次：幂等返回已有反做单，不会反做两次（否则账会被反向算两遍）。"""
    doc_id, t0 = _confirm(seed)
    with seed.database.session(context=seed.context) as session:
        first = ReversalService(session).reverse(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            doc_id=doc_id,
            member_id=seed.member_id,
            now=t0 + timedelta(minutes=1),
        )
    with seed.database.session(context=seed.context) as session:
        second = ReversalService(session).reverse(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            doc_id=doc_id,
            member_id=seed.member_id,
            now=t0 + timedelta(minutes=2),
        )
    assert second.idempotent is True
    assert second.reversal_doc_id == first.reversal_doc_id
    assert count_rows(seed.database, Reversal) == 1
    assert count_rows(seed.database, DocHeader) == 2, "重复撤产生了第二张反做单"


def test_outside_window_returns_1203(seed) -> None:
    """超过 10 分钟窗口 → ``E_REVERSAL_WINDOW_CLOSED``(1203)，引导走更正流程。"""
    doc_id, t0 = _confirm(seed)
    stale = t0 - timedelta(minutes=11)
    with seed.database.session(bypass_scope=True) as session:
        session.execute(
            update(DocHeader).where(DocHeader.doc_id == doc_id).values(created_at=stale)
        )

    with pytest.raises(AppError) as excinfo:
        with seed.database.session(context=seed.context) as session:
            ReversalService(session).reverse(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                doc_id=doc_id,
                member_id=seed.member_id,
                now=t0,
            )
    assert excinfo.value.resolved_code == "E_REVERSAL_WINDOW_CLOSED"
    assert excinfo.value.spec().numeric == 1203
    assert "sys.correct" in excinfo.value.resolved_message or "更正" in excinfo.value.resolved_message
    assert count_rows(seed.database, Reversal) == 0
    assert str(_doc(seed, doc_id).status) == "confirmed"


def test_referenced_doc_returns_1204_with_reference_chain(seed) -> None:
    """被引用（应收应付已核销）→ ``E_DOC_REFERENCED``(1204) 且**必须展示引用链**。"""
    doc_id, t0 = _confirm(seed)
    with seed.database.session(bypass_scope=True) as session:
        session.add(
            ArAp(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                partner_id=seed.partner_id,
                doc_id=doc_id,
                direction="payable",
                amount_fen=7000,
                settled_amount_fen=7000,
                status="settled",
                version=1,
            )
        )

    with pytest.raises(AppError) as excinfo:
        with seed.database.session(context=seed.context) as session:
            ReversalService(session).reverse(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                doc_id=doc_id,
                member_id=seed.member_id,
                now=t0 + timedelta(minutes=1),
            )
    assert excinfo.value.resolved_code == "E_DOC_REFERENCED"
    assert excinfo.value.spec().numeric == 1204
    references = (excinfo.value.data or {}).get("references") or []
    assert references, "拒绝撤销却没给引用链（用户不知道该先处理哪张单）"
    assert references[0]["entity"] == "ar_ap"
    assert count_rows(seed.database, Reversal) == 0


def test_readonly_intent_has_no_reversal(seed) -> None:
    """R0 只读动作**不存在撤销**：窗口为 0，且对这类单据调撤销直接拒。"""
    # 只借 ReversalService 的窗口计算（不碰 DB），所以直接塞注册表进实例
    service_window = ReversalService.__new__(ReversalService)
    service_window._registry = default_registry()
    assert service_window.window_minutes_of("stock.query") == 0
    assert service_window.window_minutes_of("purchase.create") == 10

    # 造一张 R0 意图的「单据」（真实链路不会出现，这里模拟脏数据）
    with seed.database.session(context=seed.context) as session:
        doc = DocService(session).create(
            DocDraft(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                intent_id="stock.query",
                biz_date=datetime.now(timezone.utc).date(),
                created_by=seed.member_id,
            )
        )
        dirty_doc_id = doc.doc_id

    with pytest.raises(AppError) as excinfo:
        with seed.database.session(context=seed.context) as session:
            ReversalService(session).reverse(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                doc_id=dirty_doc_id,
                member_id=seed.member_id,
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert "不支持撤销" in excinfo.value.resolved_message


def test_snapshot_take_cannot_be_reversed(seed) -> None:
    """盘点快照没有留档前值 → 不能反做（会让库存变成负数或凭空的数）。"""
    doc_id, t0 = _confirm(seed, intent_id="stock.take", c2=True)
    with pytest.raises(AppError) as excinfo:
        with seed.database.session(context=seed.context) as session:
            ReversalService(session).reverse(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                doc_id=doc_id,
                member_id=seed.member_id,
                now=t0 + timedelta(minutes=1),
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert (excinfo.value.data or {}).get("reason") == "snapshot_not_reversible"
