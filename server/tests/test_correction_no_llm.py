"""卡片编辑**不重调 AI**，且必须回流成词表/候选模板（验收⑥ 4.5）。

要证明的三件事
------------
1. **编辑是本地结构化重算**：改数量就重算金额，改单价就重算金额——`0` 次模型调用。
   证据不是「我没调」，而是 ``CountingLlmProvider.calls`` 在编辑前后**完全相等**
   （且编辑前的那次调用确实是 G4 兜底，否则这个「相等」是空证明）；
2. **纠错必须回流**：写 ``correction_event`` + upsert ``personal_dict`` +
   生成 ``template_candidate(status=pending)``，并且 ``dict_version`` 递增（击穿缓存）；
3. **回流不能直接生效**：候选模板默认 ``pending``，必须人工审核（一次误改不能污染所有人）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.dict import CorrectionEvent, PersonalDict
from app.models.rule import TemplateCandidate
from app.models.voice import ConfirmCard
from app.services.dict_layers import DictVersionCache
from app.services.ledger import LedgerService
from app.voice.llm_fallback import LlmFallback
from app.voice.llm_provider import CountingLlmProvider, FakeLlmProvider
from app.voice.pipeline import VoicePipeline
from t04_fixtures import count_rows, create_card_for, preview_for, seed  # noqa: F401  pytest 夹具

#: 这句**故意不是规则层能覆盖的写法**，把流水线逼到 G4（否则「编辑不调模型」是空证明）
G4_UTTERANCE = "李婶退回来两箱可乐"

#: G4 的预置返回（已过 Schema 的合法结果）
FAKE_LLM_RESULT = {
    "intent": "sales_return.create",
    "slots": {"P": "可乐", "Q": 2, "U": "箱"},
    "source": {"P": "asr", "Q": "asr", "U": "asr"},
    "confidence": 0.9,
}


def _pipeline_with_counter() -> tuple[VoicePipeline, CountingLlmProvider]:
    """造一条「真的会调模型」的流水线，并拿到可断言的计数器。"""
    counter = CountingLlmProvider(FakeLlmProvider(default=FAKE_LLM_RESULT))
    return VoicePipeline(fallback=LlmFallback(counter)), counter


def test_editing_a_card_does_not_call_the_model_again(seed) -> None:
    """编辑前后 ``CountingLlmProvider.calls`` 不变（编辑 0 次模型调用）。"""
    pipeline, counter = _pipeline_with_counter()
    result = pipeline.run(G4_UTTERANCE, today_iso="2026-02-01", duration_ms=2500, session_key="s-edit")
    assert result.stage == "G4", f"这句话没走到 G4，测不出「编辑不重调 AI」：stage={result.stage}"
    calls_after_parse = counter.calls
    assert calls_after_parse >= 1, "流水线没真的调模型，计数器断言失去意义"

    parse = result.parse
    assert parse is not None

    database = seed.database
    with database.session(context=seed.context) as session:
        ledger, creation = create_card_for(
            seed,
            session=session,
            intent_id=parse.intent,
            slots=dict(parse.slots),
            lines=[
                {
                    "product_id": str(seed.product_id),
                    "product_label": "可乐 500ml",
                    "quantity": 2,
                    "unit": "箱",
                    "qty_base": 48,
                    "unit_price_fen": 3500,
                    "amount_fen": 7000,
                    "payment_status": "已结",
                }
            ],
        )
        card_id = uuid.UUID(creation.card_id)
        before_edit = counter.calls

        preview = preview_for(
            seed,
            session=session,
            intent_id=parse.intent,
            lines=[
                {
                    "product_id": str(seed.product_id),
                    "product_label": "可乐 500ml",
                    "quantity": 3,
                    "unit": "箱",
                    "qty_base": 72,
                    "unit_price_fen": 3500,
                    "amount_fen": 10500,
                    "payment_status": "已结",
                }
            ],
        )
        view = ledger.edit_card(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=card_id,
            updates={"Q": 3},
            preview=preview,
        )

    # ★ 核心断言：编辑没有多调一次模型
    assert counter.calls == before_edit, "编辑卡片时又调了模型（本地重算被写成了重新解析）"

    # 编辑结果确实是「本地机械重算」出来的
    slots = {slot.code: slot for slot in view.slots}
    assert slots["Q"].value == 3
    assert slots["Q"].source == "asr", "用户亲手改的值不该再标成缺省/换算"
    assert view.lines[0].quantity == 3
    assert view.lines[0].amount_fen == 10500, "改数量后金额没有本地重算"
    assert view.blocks()["calc"]["lines"][0]["amount_fen"] == 10500


def test_correction_writes_event_dict_and_pending_candidate(seed) -> None:
    """回流三件套：``correction_event`` / ``personal_dict`` / ``template_candidate(pending)``。"""
    database = seed.database
    with database.session(context=seed.context) as session:
        ledger, creation = create_card_for(seed, session=session)
        card_id = uuid.UUID(creation.card_id)
        ledger.edit_card(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=card_id,
            updates={"P": "娃哈哈"},
        )

    assert count_rows(database, CorrectionEvent) == 1
    assert count_rows(database, PersonalDict) == 1
    assert count_rows(database, TemplateCandidate) == 1

    with database.session(bypass_scope=True) as session:
        event = session.execute(select(CorrectionEvent)).scalar_one()
        assert event.slot_code == "P"
        assert event.wrong_value == {"value": "可乐"}
        assert event.correct_value == {"value": "娃哈哈"}

        candidate = session.execute(select(TemplateCandidate)).scalar_one()
        assert str(candidate.status) == "pending", "候选模板直接生效了（一次误改会污染所有人）"
        assert candidate.intent_id == "purchase.create"
        assert "娃哈哈" in str(candidate.pattern)

        entry = session.execute(select(PersonalDict)).scalar_one()
        assert entry.raw_text == "可乐", "个人词表记的不是用户原来的说法"
        assert str(entry.layer) == "personal"


def test_dict_version_bumps_so_caches_are_invalidated() -> None:
    """教了新说法 → ``dict_version`` 必须 +1（否则词表缓存还会命旧值）。"""
    cache = DictVersionCache()
    assert cache.get("t-1", "m-1") == 0
    assert cache.bump("t-1", "m-1") == 1
    assert cache.bump("t-1", "m-1") == 2
    assert cache.get("t-1", "m-1") == 2
    # 不同租户/成员互不影响（否则一个店主教个词，全平台缓存全失效）
    assert cache.get("t-2", "m-1") == 0
    assert cache.snapshot().get("t-1:m-1") == 2


def test_edit_audit_records_zero_llm_calls(seed) -> None:
    """审计里必须写清「0 次模型调用」，让「省钱」这件事可被事后核查。"""
    from app.models.ops import AuditLog

    database = seed.database
    with database.session(context=seed.context) as session:
        ledger, creation = create_card_for(seed, session=session)
        ledger.edit_card(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=uuid.UUID(creation.card_id),
            updates={"Q": 5},
        )
    with database.session(bypass_scope=True) as session:
        rows = [row for row in session.execute(select(AuditLog)).scalars() if row.action == "card.edit"]
    assert len(rows) == 1
    detail = dict(rows[0].detail or {})
    assert detail["llm_calls"] == 0
    assert detail["changed_slots"] == ["Q"]


def test_edit_is_refused_after_confirm(seed) -> None:
    """已确认/已取消的卡片不能再编辑（改了也不会落账，等于悄悄骗用户）。"""
    from app.common.errors import AppError

    database = seed.database
    with database.session(context=seed.context) as session:
        ledger, creation = create_card_for(seed, session=session)
        card_id = uuid.UUID(creation.card_id)
        ledger.confirm(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            member_id=seed.member_id,
            card_id=card_id,
            confirm_type="button",
        )
    with database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            LedgerService(session).edit_card(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                member_id=seed.member_id,
                card_id=card_id,
                updates={"Q": 9},
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    with database.session(bypass_scope=True) as session:
        card = session.execute(select(ConfirmCard)).scalar_one()
        assert dict(card.slots)["slots"]["Q"] == 2, "已确认的卡片被改了"
