"""卡片 30s 超时：**不落账、进待确认、不重复扣次**（验收④ 4.3）。

三条不许越过的线
--------------
1. **任何情况下禁止超时自动执行**：超时只有一个结果——卡片进 ``expired`` 待确认，
   绝不会「反正时间到了就帮他记上」；
2. **不重复扣次**：复活复用**同一个** ``session_id``，``usage_ledger`` 的
   ``UNIQUE(tenant_id, session_id, event_type)`` 兜底，扣次永远只有一条；
3. **复活要真的能确认**：只把状态改回 ``active`` 是不够的——如果截止时间还停在旧值，
   一确认又立刻判过期，卡片就成了永远点不动的死卡。本文件专门钉这条。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.doc import DocHeader
from app.models.inventory import Inventory
from app.models.quota import UsageLedger, UsageQuota
from app.models.voice import ConfirmCard
from app.services.ledger import CARD_TTL_SECONDS, LedgerService, StoredCardPayload
from t04_fixtures import count_rows, create_card_for, seed  # noqa: F401  pytest 夹具


def _confirm(seed_obj, card_id: str, *, now: datetime):
    """在独立会话里确认一张卡（超时会返回 ``expired`` 结果而不是抛异常）。"""
    with seed_obj.database.session(context=seed_obj.context) as session:
        return LedgerService(session).confirm(
            tenant_id=seed_obj.tenant_id,
            store_id=seed_obj.store_id,
            member_id=seed_obj.member_id,
            card_id=uuid.UUID(card_id),
            confirm_type="button",
            button_fallback=True,
            now=now,
        )


def test_ttl_is_thirty_seconds_and_never_auto_executes(seed) -> None:
    """30s 是契约值；超时后不落账、卡片标 expired、次仍只有 1 条。"""
    assert CARD_TTL_SECONDS == 30
    database = seed.database
    t0 = datetime.now(timezone.utc)

    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session, now=t0)
        card_id = creation.card_id
        assert creation.billed is True, "建卡必须扣 1 次"
        assert creation.expires_at is not None

    # 29 秒时还在有效期内（边界内不算超时）
    with database.session(context=seed.context) as session:
        ledger = LedgerService(session)
        card = ledger._load_card(seed.tenant_id, uuid.UUID(card_id))  # noqa: SLF001 - 只读校时
        payload = StoredCardPayload.from_json(card.slots)
        assert ledger._is_expired(card, payload, t0 + timedelta(seconds=29)) is False

    # 31 秒：超时
    outcome = _confirm(seed, card_id, now=t0 + timedelta(seconds=31))
    assert outcome.expired is True
    assert outcome.error_code == "E_CARD_EXPIRED"
    assert outcome.confirmed is False
    assert outcome.doc_id is None

    assert count_rows(database, DocHeader) == 0, "超时卡片居然落了账"
    assert count_rows(database, UsageLedger) == 1, "超时不该再扣一次"
    with database.session(bypass_scope=True) as session:
        card = session.execute(select(ConfirmCard)).scalar_one()
        assert str(card.status) == "expired", "超时卡片没进待确认"
        quota = session.execute(select(UsageQuota)).scalar_one()
        assert int(quota.used_count) == 1


def test_revive_reuses_same_card_and_never_bills_twice(seed) -> None:
    """复活：同一个 ``card_id`` / ``session_id``，扣次仍是 1 条，订阅表没有第二行。"""
    database = seed.database
    t0 = datetime.now(timezone.utc)
    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session, now=t0)
        card_id, session_id = creation.card_id, creation.session_id

    _confirm(seed, card_id, now=t0 + timedelta(seconds=31))  # 先让它过期

    t1 = t0 + timedelta(seconds=40)
    with database.session(context=seed.context) as session:
        revived = LedgerService(session).revive(
            tenant_id=seed.tenant_id, session_id=uuid.UUID(session_id), now=t1
        )
    assert revived is not None
    assert revived.card_id == card_id, "复活建了一张新卡（会话唯一约束失效）"
    assert revived.billed is False, "复活又扣了一次"
    assert revived.duplicated_billing is True

    assert count_rows(database, ConfirmCard) == 1, "同一会话出现了第二张卡片"
    assert count_rows(database, UsageLedger) == 1, "复活让 usage_ledger 多了一行"

    # ★ 复活后必须真的能确认（截止时间被重写，不是「刚复活就又过期」）
    outcome = _confirm(seed, card_id, now=t1 + timedelta(seconds=5))
    assert outcome.confirmed is True, f"复活后的卡片确认不了：{outcome.as_dict()}"
    assert outcome.doc_id is not None
    assert count_rows(database, DocHeader) == 1
    assert count_rows(database, UsageLedger) == 1, "确认环节又扣了一次"


def test_same_session_cannot_create_a_second_card(seed) -> None:
    """同一 ``session_id`` 反复建卡 → 复用同一行（``UNIQUE(tenant_id, session_id)`` 兜住）。"""
    database = seed.database
    session_id = uuid.uuid4()
    with database.session(context=seed.context) as session:
        _, first = create_card_for(seed, session=session, session_id=session_id)
        _, second = create_card_for(seed, session=session, session_id=session_id)
    assert first.card_id == second.card_id
    assert second.billed is False and second.duplicated_billing is True
    assert count_rows(database, ConfirmCard) == 1
    assert count_rows(database, UsageLedger) == 1


def test_confirm_is_idempotent_after_success(seed) -> None:
    """已确认的卡片再次确认：幂等返回首次结果，**不重复写单、不重复扣次**。"""
    database = seed.database
    t0 = datetime.now(timezone.utc)
    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session, now=t0)
        card_id = creation.card_id

    first = _confirm(seed, card_id, now=t0 + timedelta(seconds=3))
    second = _confirm(seed, card_id, now=t0 + timedelta(seconds=6))

    assert first.confirmed is True
    assert second.confirmed is True
    assert second.doc_id == first.doc_id, "重复确认写出了第二张单"
    assert "幂等" in second.detail
    assert count_rows(database, DocHeader) == 1
    assert count_rows(database, UsageLedger) == 1


def test_cancel_writes_nothing_and_does_not_refund(seed) -> None:
    """取消：**不写业务库**，已扣的那次不退（建卡成功了就是一次有效服务）。"""
    database = seed.database
    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session)
        card_id = creation.card_id
        LedgerService(session).cancel(
            tenant_id=seed.tenant_id, member_id=seed.member_id, card_id=uuid.UUID(card_id)
        )
    assert count_rows(database, DocHeader) == 0
    assert count_rows(database, UsageLedger) == 1
    with database.session(bypass_scope=True) as session:
        assert str(session.execute(select(ConfirmCard)).scalar_one().status) == "cancelled"


def test_c2_without_second_voice_or_button_does_not_execute(seed) -> None:
    """C2 只点「确认」既没二次语音也没按兜底按钮 → **不执行**，且提示可以点按钮。"""
    database = seed.database
    t0 = datetime.now(timezone.utc)
    with database.session(context=seed.context) as session:
        _, creation = create_card_for(seed, session=session, intent_id="stock.take", now=t0)
        card_id = creation.card_id
        assert creation.view.confirm_policy == "C2"

    with database.session(context=seed.context) as session:
        outcome = LedgerService(session).confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(card_id),
            confirm_type="button",
            button_fallback=False,  # 没按兜底按钮
            spoken_text=None,
            now=t0 + timedelta(seconds=2),
        )
    assert outcome.needs_second_voice is True
    assert outcome.confirmed is False
    assert count_rows(database, DocHeader) == 0
    with database.session(bypass_scope=True) as session:
        assert Decimal(session.execute(select(Inventory)).scalar_one().qty_base) == seed.qty_base
