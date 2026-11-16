"""G4 LLM 兜底（**唯一计费点**）：强校验 + 最多 1 次修正重试 + 超时降级。

设计要点
--------
1. **规则层不命中才进来**：调用方（``pipeline``）保证正常流量 100% 走 G3；
2. **同一套校验器**：LLM 输出与规则层输出过**同一个** ``validate_parse_result``，
   这是「省钱不省确认」的技术保证——模型说了算的部分只到结构化，不到入账；
3. **最多 1 次修正重试**（``settings.llm_schema_retry``）：把 ``issues`` 原样回灌，
   第二次还不过就判 ``E_LLM_SCHEMA_INVALID``，**绝不假装成功**；
4. **超时/失败降级**：返回 ``E_LLM_TIMEOUT`` / ``E_INTERNAL``，``degraded=True``，
   并且 **不扣次**（``credit_consumed=False``）——用户没拿到卡片就不该付钱；
5. **结果缓存**：同一租户同一句话在 TTL 内直接复用，一次模型都不调。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain.intent_registry import IntentRegistry, default_registry
from app.voice.confidence import BAND_MID, band_of, should_card
from app.voice.llm_provider import CountingLlmProvider, LlmProviderError, LlmTimeoutError
from app.voice.output_schema import (
    ParseResult,
    SchemaValidationError,
    validate_parse_result,
)
from app.voice.prompt_builder import (
    PromptContext,
    build_static_prefix,
    build_user_block,
    static_prefix_cache_key,
)
from app.voice.prompt_cache import PromptCache, result_key

__all__ = [
    "LlmFallback",
    "LlmOutcome",
]

#: 修正重试时回灌给模型的提示前缀（把校验问题变成可执行的修改指令）
_RETRY_TEMPLATE = (
    "上一次输出不符合要求，问题如下：\n{issues}\n"
    "请只修正这些字段并重新输出完整 JSON，不要新增字段、不要解释。\n\n"
)


@dataclass
class LlmOutcome:
    """LLM 兜底结论。"""

    ok: bool
    reason: str
    result: ParseResult | None = None
    attempts: int = 0
    error_code: str | None = None
    degraded: bool = False
    #: 是否消耗了一次语音额度（**只有真正出卡才由 usage_meter 置 True**）
    credit_consumed: bool = False
    cache_hit: bool = False
    issues: tuple[str, ...] = ()
    trace: tuple[str, ...] = field(default_factory=tuple)

    @property
    def intent_id(self) -> str | None:
        """解析出的意图。"""
        return self.result.intent if self.result is not None else None


class LlmFallback:
    """LLM 兜底执行器。"""

    def __init__(
        self,
        provider: LlmProvider | CountingLlmProvider,
        *,
        cache: PromptCache | None = None,
        max_retries: int = 1,
        registry: IntentRegistry | None = None,
    ) -> None:
        """
        Args:
            provider: 可注入 provider（测试用 ``FakeLlmProvider`` + 计数包装）。
            cache: 结果/前缀缓存；``None`` 表示不缓存。
            max_retries: Schema 修正重试次数（PRD 4.4：最多 1 次）。
            registry: 意图注册表。
        """
        self._provider = provider
        self._cache = cache
        self._max_retries = max(0, max_retries)
        self._registry = registry or default_registry()

    # ------------------------------------------------------------------ 入口
    def parse(
        self,
        ctx: PromptContext,
        *,
        prefix: str | None = None,
    ) -> LlmOutcome:
        """执行一次兜底解析。

        Args:
            ctx: prompt 素材。
            prefix: 可复用的静态前缀（``None`` 时现场构造并写缓存）。

        Returns:
            :class:`LlmOutcome`；``ok=False`` 时调用方按 ``error_code`` 降级。
        """
        key = result_key(
            tenant_id=ctx.tenant_id,
            contract_version=f"{ctx.contract_version}:{self._registry.version}",
            domain=ctx.domain_hint,
            text=ctx.user_text,
        )
        if self._cache is not None:
            cached = self._cache.get(key)
            if isinstance(cached, dict):
                try:
                    model = validate_parse_result(cached, registry=self._registry)
                except SchemaValidationError:  # pragma: no cover - 缓存只存已校验结果
                    model = None
                if model is not None:
                    return LlmOutcome(
                        ok=True,
                        reason="cache_hit",
                        result=model,
                        attempts=0,
                        cache_hit=True,
                        trace=("命中结果缓存，本次未调用模型",),
                    )

        static_prefix = prefix or build_static_prefix(ctx, registry=self._registry)
        prompt = static_prefix + "\n\n[用户原话]\n" + ctx.user_text.strip()
        trace: list[str] = []

        schema = _output_schema()
        issues: tuple[str, ...] = ()
        attempts = 0
        for attempt in range(self._max_retries + 1):
            attempts = attempt + 1
            attempt_prompt = prompt
            if issues:
                attempt_prompt = _RETRY_TEMPLATE.format(issues="\n".join(f"- {item}" for item in issues)) + prompt
                trace.append(f"第 {attempts} 次尝试：回灌 {len(issues)} 条校验问题")
            try:
                raw = self._provider.parse(attempt_prompt, schema)
            except LlmTimeoutError:
                return LlmOutcome(
                    ok=False,
                    reason="llm_timeout",
                    attempts=attempts,
                    error_code="E_LLM_TIMEOUT",
                    degraded=True,
                    issues=issues,
                    trace=tuple(trace + ["模型超时，降级处理（不扣次）"]),
                )
            except LlmProviderError as exc:
                return LlmOutcome(
                    ok=False,
                    reason="llm_error",
                    attempts=attempts,
                    error_code=getattr(exc, "error_code", "E_INTERNAL"),
                    degraded=True,
                    issues=issues,
                    trace=tuple(trace + [f"模型调用失败：{exc}（不扣次）"]),
                )

            try:
                model = validate_parse_result(raw, registry=self._registry)
            except SchemaValidationError as exc:
                issues = tuple(exc.issues)
                trace.append(f"Schema 校验失败：{exc}")
                if attempt < self._max_retries:
                    if isinstance(self._provider, CountingLlmProvider):
                        self._provider.stats.schema_retries += 1
                    continue
                return LlmOutcome(
                    ok=False,
                    reason="schema_invalid",
                    attempts=attempts,
                    error_code=SchemaValidationError.error_code,
                    issues=issues,
                    trace=tuple(trace + ["修正重试已用尽，判定为非法输出（不扣次）"]),
                )

            final = self._finalize(model, ctx)
            if self._cache is not None:
                self._cache.set(key, final.model_dump())
            trace.append("LLM 输出通过 Schema + 语义校验")
            return LlmOutcome(
                ok=True,
                reason="llm_parsed",
                result=final,
                attempts=attempts,
                issues=issues,
                trace=tuple(trace),
            )

        # 理论不可达（循环内已 return）；保底返回失败而不是静默成功
        return LlmOutcome(  # pragma: no cover
            ok=False,
            reason="unreachable",
            attempts=attempts,
            error_code="E_LLM_SCHEMA_INVALID",
            degraded=True,
        )

    # ------------------------------------------------------------------ 内部
    def _static_prefix(self, ctx: PromptContext) -> str:
        """取静态前缀：**先查前缀缓存**，未命中才现场构造并写回。

        前缀只与「租户 + 契约版本 + domain + Top 商品」有关，
        所以同一门店连续录单时这部分 token 不需要重复计费。
        """
        if self._cache is not None:
            key = static_prefix_cache_key(ctx, registry=self._registry)
            cached = self._cache.get(key)
            if isinstance(cached, str) and cached:
                return cached
            built = build_static_prefix(ctx, registry=self._registry)
            self._cache.set(key, built)
            return built
        return build_static_prefix(ctx, registry=self._registry)

    def _finalize(self, model: ParseResult, ctx: PromptContext) -> ParseResult:
        """补服务端字段：``channel=llm``、``route``、低置信强制降级为追问。"""
        spec = self._registry.get(model.intent)
        route = _route_for_llm(model, spec, confidence=model.confidence)
        payload = model.model_dump()
        payload["channel"] = "llm"
        payload["route"] = route
        if route == "clarify" and model.clarify is None:
            payload["clarify"] = _fallback_clarify(model, spec)
        if payload["clarify"] is not None and route != "clarify":
            payload["route"] = "clarify"
        return validate_parse_result(payload, registry=self._registry)


def _route_for_llm(model: ParseResult, spec: Any, *, confidence: float) -> str:
    """LLM 结果的路由（与规则层同口径）。"""
    if model.clarify is not None:
        return "clarify"
    if not should_card(confidence):
        return "clarify"
    if spec is None:
        return "clarify"
    if spec.is_static:
        return "static"
    if spec.whitelist_blocked:
        return "pending_task"
    if spec.is_readonly:
        return "answer"
    return "card"


def _fallback_clarify(model: ParseResult, spec: Any) -> dict[str, Any] | None:
    """置信度低于 0.6 但没有 clarify 时，补一个**静态**追问（不重调模型）。"""
    from app.voice.clarifier_templates import build_clarify, pick_slot_to_ask

    missing = tuple(
        code for code in (spec.required_slots if spec is not None else ()) if code not in model.slots
    )
    slot = pick_slot_to_ask(missing, spec=spec) or "Q"
    prompt = build_clarify(spec.intent_id if spec is not None else model.intent, slot)
    return prompt.model_dump()


def _output_schema() -> Mapping[str, Any]:
    """给模型的输出 Schema（与 ``ParseResult`` 同构的**精简**声明）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "slots", "source", "confidence"],
        "properties": {
            "intent": {"type": "string"},
            "slots": {"type": "object"},
            "source": {"type": "object"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "clarify": {
                "type": ["object", "null"],
                "properties": {
                    "slot": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "minItems": 2, "maxItems": 3},
                },
            },
            "calc_traces": {"type": "array", "items": {"type": "string"}},
        },
    }


def confidence_band(confidence: float) -> str:
    """便捷函数：把置信度转成档位字符串（对外暴露，避免重复 import）。"""
    return band_of(confidence)


def below_mid(confidence: float) -> bool:
    """是否低于「可出卡」阈值。"""
    return confidence < BAND_MID
