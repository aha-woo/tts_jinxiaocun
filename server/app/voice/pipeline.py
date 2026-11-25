"""语音解析流水线：G0→G6 的**文本链路**编排（不写库、不建卡片）。

分层与职责
----------
=====  ================================  ==================================
层     模块                               本层负责
=====  ================================  ==================================
G0     ``gate``                          误触（连点）、无语音能量 → ``E_GATE_REJECT``
G1     ``gate`` + ``asr_provider``       0.8s 下限 / 15s 软 / 20s 硬 / VAD tail 300ms
G2     ``non_command_filter``            非命令、闲聊、静态意图、60s 去重
G3     ``template_engine``               **规则命中，0 次 LLM**（最大省钱点）
G4     ``llm_fallback``                  唯一计费点（规则未命中才进来）
G5     ``confidence`` + ``clarify_fsm``  分档与 ≤2 轮追问
G6     ``output_schema``                 统一 ``ParseResult``（卡片/只读/待办由 T04 出）
=====  ================================  ==================================

本模块**只做编排与短路**：任何一层给结论就立刻返回，绝不「先问问模型再说」。
``PipelineResult.llm_calls`` 是验收「18 个 ``template_hit`` 意图 100% 不调 LLM」
的直接证据（来自 ``CountingLlmProvider.calls`` 的增量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.domain.intent_registry import IntentRegistry, default_registry
from app.voice.asr_calibration import calibrate_text
from app.voice.clarify_fsm import MAX_CLARIFY_ROUNDS, ClarifyFSM, ClarifyState
from app.voice.confidence import band_of
from app.voice.gate import Gate, GateDecision
from app.voice.llm_fallback import LlmFallback, LlmOutcome
from app.voice.llm_provider import CountingLlmProvider
from app.voice.non_command_filter import FilterDecision, NonCommandFilter
from app.voice.output_schema import ParseResult, validate_parse_result
from app.voice.prompt_builder import PromptBudget, PromptContext, summarize_contexts
from app.voice.template_engine import TemplateEngine, TemplateOutcome

__all__ = [
    "PipelineResult",
    "VoicePipeline",
]

#: 静态意图的本地话术（**不进模型**，也不从数据库读，纯静态）
_STATIC_ANSWERS: dict[str, str] = {
    "sys.help": (
        "你说一句就行，比如「进了两箱可乐，一件三十五」「卖了三条烟，一共四百六」"
        "「盘点，可乐还剩 8 箱」。我记完会给你一张卡片，你点确认才入账。"
    ),
    "sys.example": (
        "举个例子：「今天进了两箱可乐，一件三十五」——我会记成 采购入库 2 箱可乐，"
        "单价 ¥35.00，合计 ¥70.00，日期今天。"
    ),
}


@dataclass
class PipelineResult:
    """一次流水线的最终结论（含每层可解释信息）。"""

    #: 停在哪一层：``G0``/``G1``/``G2``/``G3``/``G4``/``G5``/``G6``/``rejected``
    stage: str
    reason: str
    parse: ParseResult | None = None
    error_code: str | None = None
    #: 降级标记（超时/失败但服务可用）
    degraded: bool = False
    #: 本次消耗的 LLM 调用次数（**验收③的断言对象**）
    llm_calls: int = 0
    #: 是否命中规则层（True 表示 0 LLM）
    rule_hit: bool = False
    #: 是否消耗语音额度（只有真正出卡才由 ``usage_meter`` 置 True）
    credit_consumed: bool = False
    asr_text: str = ""
    asr_confidence: float = 0.0
    corrections: int = 0
    clarify_state: ClarifyState | None = None
    static_answer: str | None = None
    gate: GateDecision | None = None
    filter_decision: FilterDecision | None = None
    template: TemplateOutcome | None = None
    llm: LlmOutcome | None = None
    trace: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """是否产出了可用解析结果（或被有意拦下且带明确错误码）。"""
        return self.parse is not None

    @property
    def intent_id(self) -> str | None:
        """最终意图。"""
        return self.parse.intent if self.parse is not None else None

    @property
    def route(self) -> str | None:
        """最终路由。"""
        return self.parse.route if self.parse is not None else None

    @property
    def band(self) -> str | None:
        """置信度档位。"""
        return band_of(self.parse.confidence) if self.parse is not None else None


class VoicePipeline:
    """六层漏斗编排器（可注入，测试零联网）。"""

    def __init__(
        self,
        *,
        engine: TemplateEngine | None = None,
        fallback: LlmFallback | None = None,
        non_command_filter: NonCommandFilter | None = None,
        gate: Gate | None = None,
        clarify: ClarifyFSM | None = None,
        registry: IntentRegistry | None = None,
        provider: Any = None,
        budget: PromptBudget | None = None,
        contract_version: str = "0",
        store_name: str = "主仓",
        store_names: Sequence[str] = (),
    ) -> None:
        """
        Args:
            engine: G3 引擎（``None`` 时用默认规则表）。
            fallback: G4 兜底（``None`` 时视为**无 LLM 能力**，未命中直接降级）。
            non_command_filter: G2 过滤器。
            gate: G0/G1 闸门。
            clarify: G5 状态机。
            registry: 意图注册表。
            provider: LLM provider（用于读取 ``calls`` 计数，证明「没调」）。
            budget: prompt 注入预算。
            contract_version: 契约版本（参与 prompt 缓存 key）。
            store_name: 当前门店名（``W`` 的缺省来源）。
            store_names: 租户全部门店名（冲突 C12 用）。
        """
        self._registry = registry or default_registry()
        self._engine = engine or TemplateEngine(
            registry=self._registry, store_name=store_name, store_names=store_names
        )
        self._fallback = fallback
        self._filter = non_command_filter or NonCommandFilter()
        self._gate = gate or Gate()
        self._clarify = clarify or ClarifyFSM(registry=self._registry)
        self._provider = provider
        self._budget = budget or PromptBudget()
        self._contract_version = contract_version
        self._store_name = store_name
        self._store_names = tuple(store_names)

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        text: str,
        *,
        today_iso: str,
        duration_ms: int = 2000,
        now_ms: int = 0,
        last_trigger_ms: int | None = None,
        has_energy: bool = True,
        mode: str = "tap",
        session_key: str = "default",
        asr_confidence: float = 0.95,
        resolver: Any = None,
        customers: Any = None,
        suppliers: Any = None,
        last_date_iso: str | None = None,
        recent_contexts: Sequence[Any] = (),
        tenant_id: str = "-",
        member_id: str = "",
        dict_version: int = 0,
        personal_fixes: Mapping[str, str] | None = None,
        skip_gate: bool = False,
    ) -> PipelineResult:
        """跑完整文本链路（G0 → G6）。

        Args:
            text: ASR 原文（**未做同音纠正**；本方法内部会纠正）。
            today_iso: 业务日。
            duration_ms: 录音时长（G0/G1 用）。
            now_ms: 当前时间戳（误触窗口用）。
            last_trigger_ms: 上次触发时间（误触判定）。
            has_energy: 是否检测到语音能量。
            mode: ``tap`` / ``hold`` / ``vad``。
            session_key: 去重与会话冷却的 key。
            asr_confidence: ASR 置信度。
            resolver / customers / suppliers: 商品与往来单位词表。
            last_date_iso: 最近一单日期。
            recent_contexts: 最近上下文（注入 G4）。
            tenant_id: 租户 id（prompt 缓存隔离）。
            skip_gate: 测试/纯文本入口可跳过 G0/G1。

        Returns:
            :class:`PipelineResult`
        """
        calls_before = self._llm_calls()
        trace: list[str] = []

        # ---- G0/G1：误触与时长 ----
        gate_decision: GateDecision | None = None
        if not skip_gate:
            gate_decision = self._gate.check(
                duration_ms=duration_ms,
                now_ms=now_ms,
                last_trigger_ms=last_trigger_ms,
                has_energy=has_energy,
                valid_hanzi=bool((text or "").strip()),
                mode=mode,
            )
            trace.append(f"G0/G1: {gate_decision.action}/{gate_decision.reason}")
            if not gate_decision.passed:
                return PipelineResult(
                    stage="G0",
                    reason=gate_decision.reason,
                    error_code=gate_decision.error_code,
                    gate=gate_decision,
                    asr_text=text,
                    asr_confidence=asr_confidence,
                    llm_calls=self._llm_calls() - calls_before,
                    trace=tuple(trace),
                )

        # ---- ASR 后处理：同音纠正 + 个人词表替换 ----
        calibration = calibrate_text(text, personal_fixes=personal_fixes)
        normalized = calibration.text
        correction_count = len(calibration.corrections)
        adjusted_confidence = _adjust(asr_confidence, correction_count)
        if correction_count:
            trace.append(f"同音纠正 {correction_count} 处 → {normalized}")

        # ---- G2：非命令过滤（严格短路顺序）----
        filter_decision = self._filter.check(normalized, session_key=session_key)
        trace.append(f"G2: {filter_decision.action}/{filter_decision.reason}")
        if filter_decision.action == "reject":
            return PipelineResult(
                stage="G2",
                reason=filter_decision.reason,
                error_code=filter_decision.error_code or "E_NON_COMMAND",
                filter_decision=filter_decision,
                gate=gate_decision,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace),
            )
        if filter_decision.action == "duplicate":
            return PipelineResult(
                stage="G2",
                reason=filter_decision.reason,
                error_code=filter_decision.error_code or "E_FILTERED_DUP",
                filter_decision=filter_decision,
                gate=gate_decision,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace),
            )
        if filter_decision.action == "static" and filter_decision.intent_id:
            result = self._static_result(filter_decision.intent_id, adjusted_confidence)
            trace.append(f"G2: 静态意图 {filter_decision.intent_id}（本地内容，0 LLM）")
            return PipelineResult(
                stage="G2",
                reason="static_intent",
                parse=result,
                filter_decision=filter_decision,
                gate=gate_decision,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                static_answer=_STATIC_ANSWERS.get(filter_decision.intent_id),
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace),
            )

        # ---- G3：规则层（0 LLM）----
        template = self._engine.run(
            normalized,
            today_iso=today_iso,
            resolver=resolver,
            customers=customers,
            suppliers=suppliers,
            last_date_iso=last_date_iso,
            asr_confidence=adjusted_confidence,
            corrections=correction_count,
        )
        if template.hit:
            trace.append(f"G3: {template.intent_id} route={template.route}")
            state = None
            if template.clarify is not None and template.result is not None:
                state = self._clarify.begin(template.result)
            return PipelineResult(
                stage="G3",
                reason=template.reason,
                parse=template.result,
                rule_hit=True,
                gate=gate_decision,
                filter_decision=filter_decision,
                template=template,
                clarify_state=state,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace),
            )
        trace.append(f"G3: 未命中（{template.reason}）→ 升级 G4")

        # ---- G4：LLM 兜底（唯一计费点）----
        if self._fallback is None:
            return PipelineResult(
                stage="G4",
                reason="llm_disabled",
                error_code="E_INTERNAL",
                degraded=True,
                gate=gate_decision,
                filter_decision=filter_decision,
                template=template,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace + ["未配置 LLM provider，直接降级（不扣次）"]),
            )

        ctx = PromptContext(
            user_text=normalized,
            today_iso=today_iso,
            store_name=self._store_name,
            tenant_id=tenant_id,
            member_id=member_id,
            dict_version=dict_version,
            contract_version=self._contract_version,
            domain_hint=template.domain_hint,
            products=_products_of(resolver),
            personal_aliases=dict(getattr(resolver, "personal_aliases", {}) or {}),
            recent_contexts=summarize_contexts(recent_contexts),
            budget=self._budget,
            store_names=self._store_names,
        )
        outcome = self._fallback.parse(ctx)
        trace.append(f"G4: ok={outcome.ok} attempts={outcome.attempts} reason={outcome.reason}")
        if not outcome.ok:
            return PipelineResult(
                stage="G4",
                reason=outcome.reason,
                error_code=outcome.error_code,
                degraded=True,
                gate=gate_decision,
                filter_decision=filter_decision,
                template=template,
                llm=outcome,
                asr_text=normalized,
                asr_confidence=adjusted_confidence,
                corrections=correction_count,
                llm_calls=self._llm_calls() - calls_before,
                trace=tuple(trace),
            )

        # ---- G5/G6：追问与最终结果 ----
        state = None
        if outcome.result is not None and outcome.result.clarify is not None:
            state = self._clarify.begin(outcome.result)
        return PipelineResult(
            stage="G4",
            reason=outcome.reason,
            parse=outcome.result,
            gate=gate_decision,
            filter_decision=filter_decision,
            template=template,
            llm=outcome,
            clarify_state=state,
            asr_text=normalized,
            asr_confidence=adjusted_confidence,
            corrections=correction_count,
            llm_calls=self._llm_calls() - calls_before,
            trace=tuple(trace),
        )

    # ------------------------------------------------------------------ 追问
    def answer_clarify(
        self,
        state: ClarifyState,
        value: Any,
        *,
        slot: str | None = None,
        base: ParseResult | None = None,
    ) -> PipelineResult:
        """处理一次澄清回答（**纯本地**，不重调 AI）。

        Args:
            state: 澄清状态。
            value: 用户点选/说出的值。
            slot: 指定槽位（默认当前待答）。
            base: 原始解析结果（用于保留 confidence / calc_traces）。

        Returns:
            :class:`PipelineResult`；``error_code=E_CLARIFY_LIMIT`` 表示 2 轮用尽。
        """
        calls_before = self._llm_calls()
        updated = self._clarify.answer(state, value, slot=slot)
        parse = self._clarify.to_parse_result(updated, base=base)
        return PipelineResult(
            stage="G5",
            reason="clarify_answered" if not updated.exhausted else "clarify_limit",
            parse=parse,
            error_code=updated.error_code,
            clarify_state=updated,
            llm_calls=self._llm_calls() - calls_before,
            trace=(
                f"G5: round={updated.round_used}/{MAX_CLARIFY_ROUNDS} "
                f"slot={slot or updated.pending_slot} finished={updated.finished}",
            ),
        )

    # ------------------------------------------------------------------ 内部
    def _llm_calls(self) -> int:
        """当前累计 LLM 调用次数（无 provider 时为 0）。"""
        if isinstance(self._provider, CountingLlmProvider):
            return self._provider.calls
        return 0

    def _static_result(self, intent_id: str, confidence: float) -> ParseResult:
        """静态意图结果（本地话术，不调 AI、不写库）。"""
        payload = {
            "intent": intent_id,
            "slots": {},
            "source": {},
            "confidence": max(0.9, confidence),
            "clarify": None,
            "calc_traces": [],
            "channel": "template",
            "unknown_spans": [],
            "route": "static",
        }
        return validate_parse_result(payload, registry=self._registry)


def _products_of(resolver: Any) -> tuple[Any, ...]:
    """取词表里的商品 TopN 候选（注入 G4 用；拿不到就返回空，绝不猜）。"""
    lexicon = getattr(resolver, "lexicon", None)
    entries = getattr(lexicon, "entries", None)
    return tuple(entries) if entries else ()


def _adjust(confidence: float, corrections: int) -> float:
    """同音纠正后的置信度（每处 -0.05，最多 -0.15）。"""
    from app.voice.asr_calibration import adjust_confidence

    return adjust_confidence(confidence, corrections=corrections)
