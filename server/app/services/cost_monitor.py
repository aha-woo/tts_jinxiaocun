"""成本监控：把「省钱」变成可观测的五个数字（T03 内部聚合 + 单测，T04 接 API）。

五个必须能报出来的指标
----------------------
1. **规则层命中率**：``rule_hits / total_requests`` —— 直接等于「不花模型钱的比例」；
2. **LLM 调用率**：``llm_calls_requests / total_requests`` —— 应该越低越好；
3. **prompt 缓存命中率**：来自 :class:`~app.voice.prompt_cache.PromptCache`；
4. **平均 tokens**（in/out）—— 判断 prompt 是否膨胀；
5. **单次成本**（分）—— ``tokens × 单价``，**单价是假设值，需核实**。

口径说明（避免两个数字打架）
----------------------------
- ``total_requests`` 只统计**真正进入解析链路**的请求（G0/G1 拦下的误触不计，
  否则命中率会被「手抖连点」稀释得看不出真实水平）；
- 一次请求可能调 1 次模型，也可能因 Schema 修正重试调 2 次 ——
  指标同时给「请求数」与「调用次数」，两种口径都留着，不做粉饰。

本模块纯内存聚合，不查库、不发请求；生产可由 T04 定期快照落库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "CostMonitor",
    "CostSnapshot",
    "CostRates",
    "DEFAULT_COST_RATES",
]

#: 单价（分 / 千 token）【假设，需核实：以厂商实际报价替换】
DEFAULT_COST_RATES: Mapping[str, float] = {
    "input_fen_per_1k": 0.15,
    "output_fen_per_1k": 0.60,
}


@dataclass(frozen=True)
class CostRates:
    """单价表（分 / 千 token）。"""

    input_fen_per_1k: float = DEFAULT_COST_RATES["input_fen_per_1k"]
    output_fen_per_1k: float = DEFAULT_COST_RATES["output_fen_per_1k"]

    def cost_fen(self, *, tokens_in: int, tokens_out: int) -> int:
        """按 token 数算成本（向上取整到分，宁高不低）。"""
        raw = (tokens_in / 1000.0) * self.input_fen_per_1k + (
            tokens_out / 1000.0
        ) * self.output_fen_per_1k
        return int(raw) + (1 if raw - int(raw) > 1e-9 else 0)

    def as_dict(self) -> Mapping[str, float]:
        """拍平（报表用）。"""
        return {
            "input_fen_per_1k": self.input_fen_per_1k,
            "output_fen_per_1k": self.output_fen_per_1k,
        }


@dataclass
class CostSnapshot:
    """一次快照（可直接序列化进 API）。"""

    total_requests: int = 0
    rule_hits: int = 0
    llm_requests: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    llm_timeouts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cost_fen: int = 0
    by_stage: Mapping[str, int] = field(default_factory=dict)
    by_error: Mapping[str, int] = field(default_factory=dict)

    # ---- 派生指标 ----
    @property
    def rule_hit_rate(self) -> float:
        """规则层命中率（= 不花模型钱的比例）。"""
        return _ratio(self.rule_hits, self.total_requests)

    @property
    def llm_call_rate(self) -> float:
        """LLM 调用率（按请求口径）。"""
        return _ratio(self.llm_requests, self.total_requests)

    @property
    def llm_error_rate(self) -> float:
        """LLM 失败率（按调用口径）。"""
        return _ratio(self.llm_failures + self.llm_timeouts, max(1, self.llm_calls))

    @property
    def prompt_cache_hit_rate(self) -> float:
        """prompt/结果缓存命中率。"""
        return _ratio(self.cache_hits, self.cache_hits + self.cache_misses)

    @property
    def avg_tokens_in(self) -> float:
        """平均输入 token（按调用）。"""
        return round(self.tokens_in / self.llm_calls, 2) if self.llm_calls else 0.0

    @property
    def avg_tokens_out(self) -> float:
        """平均输出 token（按调用）。"""
        return round(self.tokens_out / self.llm_calls, 2) if self.llm_calls else 0.0

    @property
    def cost_per_request_fen(self) -> float:
        """单次请求平均成本（分）。"""
        return round(self.cost_fen / self.total_requests, 4) if self.total_requests else 0.0

    def as_dict(self) -> Mapping[str, Any]:
        """拍平（API/报表/单测断言共用一份结构）。"""
        return {
            "total_requests": self.total_requests,
            "rule_hits": self.rule_hits,
            "rule_hit_rate": self.rule_hit_rate,
            "llm_requests": self.llm_requests,
            "llm_call_rate": self.llm_call_rate,
            "llm_calls": self.llm_calls,
            "llm_failures": self.llm_failures,
            "llm_timeouts": self.llm_timeouts,
            "llm_error_rate": self.llm_error_rate,
            "prompt_cache_hit_rate": self.prompt_cache_hit_rate,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_tokens_in": self.avg_tokens_in,
            "avg_tokens_out": self.avg_tokens_out,
            "cost_fen": self.cost_fen,
            "cost_per_request_fen": self.cost_per_request_fen,
            "by_stage": dict(self.by_stage),
            "by_error": dict(self.by_error),
        }


class CostMonitor:
    """成本聚合器：喂 ``PipelineResult``，吐上面那五个数字。"""

    def __init__(
        self,
        *,
        rates: CostRates | None = None,
        prompt_cache: Any = None,
    ) -> None:
        """
        Args:
            rates: token 单价。
            prompt_cache: :class:`~app.voice.prompt_cache.PromptCache`（可选，用于报缓存命中率）。
        """
        self._rates = rates or CostRates()
        self._cache = prompt_cache
        self._total = 0
        self._rule_hits = 0
        self._llm_requests = 0
        self._llm_calls = 0
        self._llm_failures = 0
        self._llm_timeouts = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._cost_fen = 0
        self._by_stage: dict[str, int] = {}
        self._by_error: dict[str, int] = {}

    # ------------------------------------------------------------------ 采集
    def record(
        self,
        *,
        stage: str,
        rule_hit: bool = False,
        llm_calls: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error_code: str | None = None,
        degraded: bool = False,
        counted: bool = True,
    ) -> None:
        """记录一次解析请求。

        Args:
            stage: 停在 G0..G6 的哪一层。
            rule_hit: 是否命中规则层。
            llm_calls: 本次的模型调用次数（重试也计入）。
            tokens_in / tokens_out: token 数。
            error_code: 错误码（有则计入 by_error）。
            degraded: 是否降级返回。
            counted: 是否计入「解析请求总数」（G0/G1 误触传 ``False``）。
        """
        if counted:
            self._total += 1
        self._by_stage[stage] = self._by_stage.get(stage, 0) + 1
        if rule_hit:
            self._rule_hits += 1
        if llm_calls > 0:
            self._llm_requests += 1
            self._llm_calls += llm_calls
            self._tokens_in += max(0, tokens_in)
            self._tokens_out += max(0, tokens_out)
            self._cost_fen += self._rates.cost_fen(tokens_in=tokens_in, tokens_out=tokens_out)
        if error_code:
            self._by_error[error_code] = self._by_error.get(error_code, 0) + 1
            if error_code == "E_LLM_TIMEOUT":
                self._llm_timeouts += 1
            elif error_code == "E_LLM_SCHEMA_INVALID":
                self._llm_failures += 1
        elif degraded:
            self._llm_failures += 1

    def record_pipeline(self, result: Any, *, counted: bool = True) -> None:
        """直接吃 :class:`~app.voice.pipeline.PipelineResult`（T04 唯一调用点）。"""
        self.record(
            stage=getattr(result, "stage", "unknown"),
            rule_hit=bool(getattr(result, "rule_hit", False)),
            llm_calls=int(getattr(result, "llm_calls", 0)),
            tokens_in=_usage(getattr(result, "llm", None), "tokens_in"),
            tokens_out=_usage(getattr(result, "llm", None), "tokens_out"),
            error_code=getattr(result, "error_code", None),
            degraded=bool(getattr(result, "degraded", False)),
            counted=counted,
        )

    # ------------------------------------------------------------------ 输出
    def snapshot(self) -> CostSnapshot:
        """当前快照（含缓存命中率）。"""
        cache_hits, cache_misses = 0, 0
        if self._cache is not None:
            stats = getattr(self._cache, "stats", None)
            if callable(stats):
                cache_stats = stats()
                cache_hits = int(cache_stats.get("hits", 0))
                cache_misses = int(cache_stats.get("misses", 0))
        return CostSnapshot(
            total_requests=self._total,
            rule_hits=self._rule_hits,
            llm_requests=self._llm_requests,
            llm_calls=self._llm_calls,
            llm_failures=self._llm_failures,
            llm_timeouts=self._llm_timeouts,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            cost_fen=self._cost_fen,
            by_stage=dict(self._by_stage),
            by_error=dict(self._by_error),
        )

    def as_dict(self) -> Mapping[str, Any]:
        """快照拍平（API 直接返回这一坨）。"""
        snapshot = self.snapshot().as_dict()
        snapshot["rates"] = self._rates.as_dict()
        return snapshot

    def cost_ratio(self, *, budget_fen: int) -> float:
        """成本占预算比例（喂给 ``degrade.decide``）。"""
        if budget_fen <= 0:
            return 0.0
        return round(self._cost_fen / budget_fen, 4)

    def reset(self) -> None:
        """清零（按日滚动统计）。"""
        self.__init__(rates=self._rates, prompt_cache=self._cache)  # type: ignore[misc]

    def top_errors(self, *, limit: int = 5) -> tuple[tuple[str, int], ...]:
        """错误码 TopN（排查用）。"""
        return tuple(sorted(self._by_error.items(), key=lambda item: item[1], reverse=True)[:limit])

    def stages(self) -> Iterable[tuple[str, int]]:
        """各层分布（看漏斗是否按预期衰减）。"""
        return tuple(sorted(self._by_stage.items()))


def _ratio(numerator: int, denominator: int) -> float:
    """安全比例（分母为 0 返回 0）。"""
    return round(numerator / denominator, 4) if denominator else 0.0


def _usage(outcome: Any, name: str) -> int:
    """从 ``LlmOutcome`` 里取 token（没有就 0，绝不猜）。"""
    if outcome is None:
        return 0
    trace = getattr(outcome, "usage", None)
    if isinstance(trace, Mapping):
        return int(trace.get(name, 0) or 0)
    stats = getattr(outcome, "stats", None)
    if isinstance(stats, Mapping):
        return int(stats.get(name, 0) or 0)
    return 0
