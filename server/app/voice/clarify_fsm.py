"""G5 澄清状态机：**硬上限 2 轮、一次只问 1 槽、点选后只做本地结构化更新**。

三条铁律（PRD 4.3 / 架构 §4 ⑤）
------------------------------
1. **2 轮封顶**：第 3 轮不再追问，直接判 ``E_CLARIFY_LIMIT``(1005)，把话术改成
   「我先把这条记成待办，你回头点一下」——比无限追问更省更不烦人；
2. **一次只问 1 个槽位**：``ClarifyPrompt.slot`` 是单值，选项 2~3 个；
3. **点选后不重调 AI**：用户点「按盘点覆盖」就是一次**本地结构化赋值**，
   绝不重新走 G4。这条最容易被实现成「重新解析一遍」，那样澄清就变成了成本放大器。

状态机是纯内存的（``ClarifyState``），可序列化进卡片状态，
因此同一张卡片被点两次也不会产生副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.domain.intent_registry import IntentRegistry, IntentSpec, default_registry
from app.voice.clarifier_templates import (
    ClarifierCatalog,
    build_clarify,
    default_catalog,
    pick_slot_to_ask,
)
from app.voice.output_schema import ClarifyPrompt, ParseResult, validate_parse_result
from app.utils.unit import normalize_unit

__all__ = [
    "MAX_CLARIFY_ROUNDS",
    "ClarifyState",
    "ClarifyFSM",
    "interpret_answer",
]

#: 追问轮数**硬上限**（超过即 ``E_CLARIFY_LIMIT``，不允许「再问一句」）
MAX_CLARIFY_ROUNDS = 2

#: 追问超限时的用户可见话术（静态，不调 AI）
CLARIFY_LIMIT_MESSAGE = "我先帮你记成待办，回头点一下就能补全。"

#: 各槽位的可接受裸值转换（点选回填时用，**不做语义猜测**）
_ENUM_SLOTS: Mapping[str, frozenset[str]] = {
    "PS": frozenset({"已结", "未结", "部分"}),
    "TF": frozenset({"进价", "售价", "联系人", "电话", "名称", "备注"}),
}


@dataclass
class ClarifyState:
    """一次澄清会话的状态（可序列化，无副作用）。"""

    intent_id: str
    slots: dict[str, Any] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)
    #: 已问过的轮数
    round_used: int = 0
    #: 当前待回答的槽位
    pending_slot: str | None = None
    #: 当前追问（含 2~3 个可点选项）
    prompt: ClarifyPrompt | None = None
    finished: bool = False
    exhausted: bool = False
    error_code: str | None = None
    #: 已回答的历史 ``[(slot, value)]``
    answers: list[tuple[str, Any]] = field(default_factory=list)
    #: 每轮话术快照（审计/调试用）
    transcript: list[str] = field(default_factory=list)

    @property
    def remaining_rounds(self) -> int:
        """还能问几轮。"""
        return max(0, MAX_CLARIFY_ROUNDS - self.round_used)

    @property
    def can_ask(self) -> bool:
        """是否还能继续追问。"""
        return not self.finished and not self.exhausted and self.remaining_rounds > 0


class ClarifyFSM:
    """澄清状态机（一次只问 1 槽、最多 2 轮）。"""

    def __init__(
        self,
        *,
        registry: IntentRegistry | None = None,
        catalog: ClarifierCatalog | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._catalog = catalog or default_catalog()

    # ------------------------------------------------------------------ 起始
    def begin(self, result: ParseResult) -> ClarifyState:
        """从一次解析结果开启澄清会话（``result.clarify`` 已给出首轮问题）。"""
        spec = self._registry.get(result.intent)
        state = ClarifyState(
            intent_id=result.intent,
            slots=dict(result.slots),
            source=dict(result.source),
        )
        if result.clarify is not None:
            state.prompt = result.clarify
            state.pending_slot = result.clarify.slot
            state.transcript.append(result.clarify.question)
            return state
        prompt = self._next_prompt(state, spec)
        if prompt is None:
            state.finished = True
            return state
        state.prompt = prompt
        state.pending_slot = prompt.slot
        state.transcript.append(prompt.question)
        return state

    # ------------------------------------------------------------------ 回答
    def answer(
        self,
        state: ClarifyState,
        value: Any,
        *,
        slot: str | None = None,
    ) -> ClarifyState:
        """用户回答/点选后**本地**更新槽位（**不重调 AI**）。

        Args:
            state: 当前状态。
            value: 用户点选的选项文本或直接给的值。
            slot: 指定槽位（默认用 ``state.pending_slot``）。

        Returns:
            更新后的状态（同一个对象，便于链式调用）。

        Raises:
            ValueError: 状态已结束或没有待答槽位（调用方应先看 ``can_ask``）。
        """
        if state.finished:
            raise ValueError("澄清会话已结束，不能再回答")
        target = slot or state.pending_slot
        if target is None:
            raise ValueError("没有待回答的槽位")
        if state.round_used >= MAX_CLARIFY_ROUNDS:
            state.exhausted = True
            state.error_code = "E_CLARIFY_LIMIT"
            state.finished = True
            return state

        spec = self._registry.get(state.intent_id)
        parsed, ok = interpret_answer(target, value, spec=spec)
        if ok:
            state.slots[target] = parsed
            state.source[target] = "asr"
            state.answers.append((target, parsed))
        else:
            state.answers.append((target, None))

        state.round_used += 1
        state.prompt = None
        state.pending_slot = None

        if self._complete(state, spec):
            state.finished = True
            return state

        if state.remaining_rounds <= 0:
            state.exhausted = True
            state.error_code = "E_CLARIFY_LIMIT"
            state.finished = True
            state.transcript.append(CLARIFY_LIMIT_MESSAGE)
            return state

        prompt = self._next_prompt(state, spec)
        if prompt is None:
            state.finished = True
            return state
        state.prompt = prompt
        state.pending_slot = prompt.slot
        state.transcript.append(prompt.question)
        return state

    # ------------------------------------------------------------------ 输出
    def to_parse_result(self, state: ClarifyState, *, base: ParseResult | None = None) -> ParseResult:
        """把状态转回 ``ParseResult``（走同一个校验器）。"""
        confidence = base.confidence if base is not None else 0.5
        payload: dict[str, Any] = {
            "intent": state.intent_id,
            "slots": dict(state.slots),
            "source": dict(state.source),
            "confidence": confidence,
            "clarify": state.prompt.model_dump() if state.prompt is not None else None,
            "calc_traces": list(base.calc_traces) if base is not None else [],
            "channel": base.channel if base is not None else None,
            "unknown_spans": list(base.unknown_spans) if base is not None else [],
            "route": (
                "clarify"
                if state.prompt is not None
                else ("pending_task" if state.exhausted else None)
            ),
        }
        if payload["route"] is None:
            payload["route"] = "card"
        return validate_parse_result(payload, registry=self._registry)

    # ------------------------------------------------------------------ 内部
    def _complete(self, state: ClarifyState, spec: IntentSpec | None) -> bool:
        """必填槽位是否已齐。"""
        if spec is None:
            return True
        return all(code in state.slots for code in spec.required_slots)

    def _next_prompt(
        self, state: ClarifyState, spec: IntentSpec | None
    ) -> ClarifyPrompt | None:
        """挑下一个要问的槽位（一次 1 个，选项 2~3 个）。"""
        if spec is None:
            return None
        missing = [code for code in spec.required_slots if code not in state.slots]
        slot = pick_slot_to_ask(missing, spec=spec)
        if slot is None:
            return None
        return build_clarify(spec.intent_id, slot, catalog=self._catalog)


def interpret_answer(
    slot: str,
    value: Any,
    *,
    spec: IntentSpec | None = None,
) -> tuple[Any, bool]:
    """把用户的**点选文本**转成槽位值（纯本地转换，不调 AI）。

    Args:
        slot: 槽位代号。
        value: 用户给的值（选项文本或数字）。
        spec: 当前意图（``Q`` 的 0 值校验需要）。

    Returns:
        ``(转换后的值, 是否成功)``；失败时值为 ``None``，调用方应保持原状继续问。
    """
    del spec  # 保留参数：将来按 intent 做更细的值域校验
    if value is None:
        return None, False
    if slot in _ENUM_SLOTS:
        text = str(value).strip()
        return (text, True) if text in _ENUM_SLOTS[slot] else (None, False)
    if slot == "U":
        unit = normalize_unit(str(value))
        return (unit, True) if unit else (None, False)
    if slot in {"Q", "RATIO"}:
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            return None, False
        return (int(number) if number.is_integer() else number, True)
    if slot == "D":
        text = str(value).strip()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return text, True
        return None, False
    text = str(value).strip()
    return (text, True) if text else (None, False)


def options_of(prompt: ClarifyPrompt | None) -> Sequence[str]:
    """取追问选项（便捷函数，便于测试断言 2~3 个）。"""
    return tuple(prompt.options) if prompt is not None else ()
