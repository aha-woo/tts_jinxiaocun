"""G5 澄清：**硬上限 2 轮、一次只问 1 槽、2~3 个选项、点选后不重调 AI**（验收项⑤）。

为什么这三条要写成测试而不是注释
--------------------------------
- 追问轮数一旦失控，用户会被问到崩溃，成本也翻倍（每轮都可能触发一次模型）；
- 「一次问多个槽位」会让点选回填变成猜谜；
- **点选后重调 AI 是最隐蔽的成本放大器**：看起来只是「重新理解一下」，
  实际把一次澄清变成一次完整计费。所以这里直接断言 ``counter.calls`` 不变。
"""

from __future__ import annotations

import pytest

from app.domain.intent_registry import default_registry
from app.voice.clarify_fsm import (
    CLARIFY_LIMIT_MESSAGE,
    MAX_CLARIFY_ROUNDS,
    ClarifyFSM,
    interpret_answer,
)
from app.voice.clarifier_templates import MAX_OPTIONS, MIN_OPTIONS, build_clarify
from app.voice.output_schema import ClarifyPrompt
from voice_demo import TODAY, build_pipeline, demo_resolver

#: 追问轮数硬上限必须是 2（PRD 4.3）
assert MAX_CLARIFY_ROUNDS == 2


def test_clarify_prompt_has_one_slot_and_two_to_three_options() -> None:
    """追问必须**只问一个槽位**且给 2~3 个可点选项。"""
    prompt = build_clarify("purchase.create", "Q")
    assert isinstance(prompt, ClarifyPrompt)
    assert isinstance(prompt.slot, str) and prompt.slot == "Q"
    assert MIN_OPTIONS <= len(prompt.options) <= MAX_OPTIONS
    assert prompt.question.strip()


@pytest.mark.parametrize(
    "intent_id",
    [
        "purchase.create",
        "sales.create",
        "stock.take",
        "stock.loss",
        "debt.query",
        "product.create",
        "payment.receive",
    ],
)
def test_every_clarifier_is_static_and_within_bounds(intent_id: str) -> None:
    """话术来自契约静态内容；选项数永远落在 2~3（不调 AI 生成）。"""
    spec = default_registry().require(intent_id)
    for slot_code in spec.clarifier_templates:
        prompt = build_clarify(intent_id, slot_code)
        assert MIN_OPTIONS <= len(prompt.options) <= MAX_OPTIONS
        # 话术必须与契约逐字一致（证明未重新发明）
        assert prompt.question == spec.clarifier_templates[slot_code]


def test_missing_required_slot_produces_clarify_instead_of_card() -> None:
    """缺必填槽位时**不出写卡**，只追问（避免猜着记账）。"""
    pipeline, counter = build_pipeline()
    result = pipeline.run(
        "今天进了两箱，一件三十五",  # 有数量有金额，但没有商品
        today_iso=TODAY,
        resolver=demo_resolver(),
        session_key="missing-p",
    )
    assert result.parse is not None
    assert result.route == "clarify"
    assert result.parse.clarify is not None
    assert result.parse.clarify.slot in {"P", "Q"}
    assert counter.calls == 0, "澄清不得调用模型"


def test_two_round_limit_then_pending_task() -> None:
    """追问**最多 2 轮**；第 3 轮不再问，转待办并给 ``E_CLARIFY_LIMIT``。"""
    fsm = ClarifyFSM()
    base = _parse_result_intent("purchase.create")

    state = fsm.begin(base)
    assert state.can_ask is True
    assert state.round_used == 0

    # 第 1 轮：答一个仍然不完整的值（故意答非所问，模拟用户说「就那样」）
    fsm.answer(state, "随便", slot="P")
    assert state.round_used == 1
    assert state.can_ask is True

    # 第 2 轮
    fsm.answer(state, "随便", slot="Q")
    assert state.round_used == 2
    assert state.can_ask is False
    assert state.exhausted is True
    assert state.error_code == "E_CLARIFY_LIMIT"
    assert state.finished is True
    assert CLARIFY_LIMIT_MESSAGE in state.transcript


def test_answering_all_required_slots_finishes_before_limit() -> None:
    """两轮内补齐必填即可完成，不会硬凑到上限。"""
    fsm = ClarifyFSM()
    base = _parse_result_intent("purchase.create")
    state = fsm.begin(base)

    fsm.answer(state, "可乐", slot="P")
    assert state.pending_slot == "Q"
    fsm.answer(state, "2", slot="Q")
    assert state.finished is True
    assert state.exhausted is False
    assert state.slots["P"] == "可乐" and state.slots["Q"] == 2


def test_clarify_answer_is_local_and_never_calls_llm() -> None:
    """用户点选后**只做本地结构化更新**：provider 调用次数不变。"""
    pipeline, counter = build_pipeline()
    result = pipeline.run(
        "今天进了两箱，一件三十五",
        today_iso=TODAY,
        resolver=demo_resolver(),
        session_key="clariy-local",
    )
    assert result.clarify_state is not None
    state = result.clarify_state
    slot = state.pending_slot or "P"

    calls_before = counter.calls
    answered = pipeline.answer_clarify(state, "可乐", slot=slot, base=result.parse)
    assert counter.calls == calls_before, "点选回填不得重调模型"
    assert answered.parse is not None
    assert answered.parse.slots[slot] == "可乐"
    assert answered.llm_calls == 0


def test_clarify_limit_surfaces_error_code_via_pipeline() -> None:
    """状态机到上限时，流水线返回 ``E_CLARIFY_LIMIT`` 供端上提示。"""
    pipeline, counter = build_pipeline()
    result = pipeline.run(
        "今天进了两箱，一件三十五",
        today_iso=TODAY,
        resolver=demo_resolver(),
        session_key="clarify-limit",
    )
    state = result.clarify_state
    assert state is not None

    # 故意两次都答「听不清」（空值），走到 2 轮上限
    pipeline.answer_clarify(state, "", slot="P", base=result.parse)
    final = pipeline.answer_clarify(state, "", slot="Q", base=result.parse)
    assert final.error_code == "E_CLARIFY_LIMIT"
    assert final.reason == "clarify_limit"
    assert counter.calls == 0


def test_interpret_answer_rejects_illegal_enum_values() -> None:
    """枚举槽位只接受三/六值枚举，非法值不写进槽位。"""
    value, ok = interpret_answer("PS", "已结")
    assert (value, ok) == ("已结", True)
    value, ok = interpret_answer("PS", "好像付了")
    assert (value, ok) == (None, False)
    value, ok = interpret_answer("U", "箱")
    assert ok is True and value == "箱"


def test_clarify_options_come_from_static_table_not_ai() -> None:
    """``PS`` 的选项就是三值枚举（静态表），不会由模型编。"""
    prompt = build_clarify("sales.create", "PS")
    assert list(prompt.options) == ["已结", "未结", "部分"]


def _parse_result_intent(intent_id: str):
    """构造一个「只剩必填缺口」的解析结果（不经过引擎，专注状态机）。"""
    from app.voice.output_schema import validate_parse_result

    spec = default_registry().require(intent_id)
    payload = {
        "intent": intent_id,
        "slots": {},
        "source": {},
        "confidence": 0.5,
        "clarify": build_clarify(intent_id, spec.required_slots[0]).model_dump(),
        "route": "clarify",
    }
    return validate_parse_result(payload)
