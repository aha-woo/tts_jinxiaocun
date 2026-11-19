"""G4 LLM provider 抽象（**唯一计费点**，可注入、可计数、可离线）。

三条硬规矩
----------
1. **可注入**：所有上层（``llm_fallback``）只依赖 :class:`LlmProvider` 协议，
   测试里换成 :class:`FakeLlmProvider` 或 :class:`CountingLlmProvider` 即可，
   **零联网**跑完整条流水线；
2. **可计数**：:class:`CountingLlmProvider` 记录 ``calls / tokens_in / tokens_out``。
   这是验收项「18 个 ``template_hit`` 意图 100% 不调 LLM」的**唯一可信证据**——
   不靠口头承诺，靠 ``assert counter.calls == 0``；
3. **误用必炸**：``LLM_PROVIDER=real`` 但没给 ``LLM_API_KEY/LLM_BASE_URL`` 时，
   :class:`RealLlmProvider` 在**构造期**就抛错（不是等第一次调用才 500），
   避免「配置写错 → 线上静默降级 / 静默烧钱」；反过来 ``FakeLlmProvider``
   在 ``env=production`` 下构造也会立刻抛错，防止假实现被带上生产。

成本口径（全部是「假设，需核实」）
---------------------------------
token 由 provider 自报（``usage``）；无 ``usage`` 时按 ``len(text)/4`` 粗估，
只用于内部成本监控，**绝不作为计费依据**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "CountingLlmProvider",
    "FakeLlmProvider",
    "LlmCallStats",
    "LlmProvider",
    "LlmProviderError",
    "LlmTimeoutError",
    "RealLlmProvider",
    "build_llm_provider",
    "estimate_tokens",
]

#: 粗估 token（仅成本监控用；厂商真实 usage 优先）
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """粗估 token 数（中文按字符数 / 4，仅供内部成本观测）。"""
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


class LlmProviderError(RuntimeError):
    """LLM 调用失败（网络/4xx/5xx/非 JSON）。"""

    error_code = "E_INTERNAL"


class LlmTimeoutError(LlmProviderError):
    """LLM 超时（映射 ``E_LLM_TIMEOUT``，降级但不扣次）。"""

    error_code = "E_LLM_TIMEOUT"


@dataclass
class LlmCallStats:
    """调用统计（成本监控 + 测试断言共用）。"""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    failures: int = 0
    timeouts: int = 0
    schema_retries: int = 0

    def record(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """记一次成功调用。"""
        self.calls += 1
        self.tokens_in += max(0, tokens_in)
        self.tokens_out += max(0, tokens_out)

    def record_failure(self, *, timeout: bool = False) -> None:
        """记一次失败（失败也计次，便于观察成本与稳定性）。"""
        self.calls += 1
        self.failures += 1
        if timeout:
            self.timeouts += 1

    def snapshot(self) -> dict[str, int]:
        """拍平成字典（``/readyz`` / 成本报表 / 测试断言用）。"""
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "schema_retries": self.schema_retries,
        }


@runtime_checkable
class LlmProvider(Protocol):
    """LLM 协议：**只做一件事**——把 prompt + schema 换成 dict。"""

    def parse(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """调用模型并返回结构化字典（未校验，校验在 ``llm_fallback``）。"""
        ...


class CountingLlmProvider:
    """计数包装器：透传给内层 provider，同时累计调用与 token。

    Examples:
        >>> counter = CountingLlmProvider(FakeLlmProvider())
        >>> counter.calls
        0
    """

    def __init__(self, inner: LlmProvider) -> None:
        self._inner = inner
        self.stats = LlmCallStats()

    @property
    def inner(self) -> LlmProvider:
        """被包装的 provider。"""
        return self._inner

    @property
    def calls(self) -> int:
        """累计调用次数（**验收③的核心断言对象**）。"""
        return self.stats.calls

    @property
    def tokens_in(self) -> int:
        """累计输入 token。"""
        return self.stats.tokens_in

    @property
    def tokens_out(self) -> int:
        """累计输出 token。"""
        return self.stats.tokens_out

    def parse(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """透传调用并按需累计（异常也要计数，否则会低估成本）。"""
        try:
            result = self._inner.parse(prompt, schema)
        except LlmTimeoutError:
            self.stats.record_failure(timeout=True)
            raise
        except LlmProviderError:
            self.stats.record_failure()
            raise
        usage = getattr(self._inner, "last_usage", None) or {}
        self.stats.record(
            tokens_in=int(usage.get("tokens_in", estimate_tokens(prompt))),
            tokens_out=int(usage.get("tokens_out", estimate_tokens(json.dumps(result, ensure_ascii=False)))),
        )
        return result

    def reset(self) -> None:
        """清零统计（长生命周期进程里按租户/按轮次统计用）。"""
        self.stats = LlmCallStats()


class UnavailableLlmProvider:
    """生产未配置模型时的占位 provider：**构造不报错，调用才报错**。

    为什么不在构造期炸（2026-11-19 线上 500 的根因）
    ----------------------------------------------
    本项目是「规则层优先」：进货 / 卖货 / 查询 / 新建商品 / 别名这些高频说法
    **根本不调模型**。如果构造期就抛，整条主链路会在装配运行时时一起被打死
    （线上表现：手动记账「进货可乐」→ `POST /parse` 500，
    `RuntimeError: FakeLlmProvider 禁止在生产环境使用`）。

    改成「调用才抛」之后：
    - 规则层命中的说法照常工作，零模型成本；
    - 真需要模型的说法 → `LlmFallback` 捕获 ``LlmProviderError`` → 降级成
      澄清/手动录入（不扣次），用户拿到能照做的提示而不是 500。

    它**绝不返回任何解析结果**，所以不存在「把假结果当成真解析」。
    """

    provider_name = "unavailable"

    def parse(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """抛 ``LlmProviderError``（由上层降级处理，绝不返回假结果）。"""
        raise LlmProviderError(
            "生产环境未配置大模型：进货/卖货/查询/新建商品/别名由规则层直接处理；"
            "这条说法需要模型理解。可以切「手动记账」，或配置 "
            "LLM_PROVIDER=real + LLM_API_KEY / LLM_BASE_URL 后重新部署"
        )


class FakeLlmProvider:
    """**仅本地与测试，生产禁用**：从 fixture 返回预置 JSON，永不联网。

    匹配规则：fixture 的 key 出现在 prompt 里即命中（按 key 长度降序，最长匹配优先）；
    都未命中时返回 ``default``；``default`` 也没有则返回 ``{}``
    （由 ``llm_fallback`` 判成 ``E_LLM_SCHEMA_INVALID``，而不是伪造结果）。
    """

    provider_name = "fake"

    def __init__(
        self,
        fixtures: Mapping[str, Mapping[str, Any] | str] | None = None,
        *,
        default: Mapping[str, Any] | None = None,
        is_production: bool = False,
    ) -> None:
        if is_production:
            raise RuntimeError(
                "FakeLlmProvider 禁止在生产环境使用（会把假结果当成真解析）；"
                "请设置 LLM_PROVIDER=real 并注入 LLM_API_KEY / LLM_BASE_URL"
            )
        self._fixtures: dict[str, Mapping[str, Any] | str] = dict(fixtures or {})
        self._default: Mapping[str, Any] = dict(default or {})
        self.last_usage: dict[str, int] = {}
        self.calls: list[str] = []

    def parse(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """按 fixture 返回预置结果（记录 prompt 便于断言「到底调没调」）。"""
        del schema  # fake 不关心 schema；真实 provider 才需要它做结构化输出
        self.calls.append(prompt)
        for key in sorted(self._fixtures, key=len, reverse=True):
            if key and key in prompt:
                payload = self._fixtures[key]
                self.last_usage = {
                    "tokens_in": estimate_tokens(prompt),
                    "tokens_out": estimate_tokens(str(payload)),
                }
                return dict(payload) if isinstance(payload, Mapping) else json.loads(payload)
        self.last_usage = {
            "tokens_in": estimate_tokens(prompt),
            "tokens_out": estimate_tokens(json.dumps(self._default, ensure_ascii=False)),
        }
        return dict(self._default)


class RealLlmProvider:
    """真实 LLM（httpx）：**构造期即校验凭证**，缺一个就炸，绝不静默降级。"""

    provider_name = "real"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 6.0,
        max_tokens: int = 1024,
    ) -> None:
        """
        Raises:
            ValueError: 缺 ``api_key`` / ``base_url`` / ``model`` —— 配置错误必须立刻暴露。
        """
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", api_key),
                ("LLM_BASE_URL", base_url),
                ("LLM_MODEL", model),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise ValueError(
                "RealLlmProvider 缺少必要配置: "
                + ", ".join(missing)
                + "（LLM_PROVIDER=real 时必须全部注入；配置错误不允许静默降级）"
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self.last_usage: dict[str, int] = {}

    def parse(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """调用 chat/completions 并解析 JSON（超时映射 ``E_LLM_TIMEOUT``）。"""
        import httpx  # 局部导入：本地/测试不需要 httpx 也能 import 本模块

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "只输出符合给定 JSON Schema 的 JSON，不要解释，不要 Markdown。",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object", "schema": dict(schema)},
            "max_tokens": self._max_tokens,
            "temperature": 0,
        }
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError(f"LLM 超时（>{self._timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:  # pragma: no cover - 网络异常分支
            raise LlmProviderError(f"LLM 请求失败: {exc}") from exc

        if response.status_code >= 400:
            raise LlmProviderError(f"LLM 返回 {response.status_code}: {response.text[:200]}")

        try:
            body = response.json()
        except ValueError as exc:
            raise LlmProviderError("LLM 响应不是合法 JSON") from exc

        usage = body.get("usage") or {}
        self.last_usage = {
            "tokens_in": int(usage.get("prompt_tokens", estimate_tokens(prompt))),
            "tokens_out": int(usage.get("completion_tokens", 0)),
        }
        content = _extract_content(body)
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmProviderError(f"LLM 输出不是合法 JSON: {content[:200]}") from exc


def _extract_content(body: Mapping[str, Any]) -> str:
    """从 chat/completions 响应里取出文本内容（兼容 choices/message/content）。"""
    choices = body.get("choices") or []
    if not choices:
        raise LlmProviderError("LLM 响应缺少 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LlmProviderError("LLM 响应 content 为空")
    return content


def build_llm_provider(
    settings: Any,
    *,
    fixtures: Mapping[str, Mapping[str, Any] | str] | None = None,
    default: Mapping[str, Any] | None = None,
    wrap_counter: bool = True,
) -> LlmProvider:
    """按 ``settings.llm_provider`` 构造 provider（唯一装配点）。

    Args:
        settings: :class:`app.config.Settings`。
        fixtures: ``FakeLlmProvider`` 的预置返回。
        default: fake 的兜底返回。
        wrap_counter: 是否套 :class:`CountingLlmProvider`（生产也建议套，便于成本监控）。

    Returns:
        provider 实例。生产 + ``fake`` 时返回 :class:`UnavailableLlmProvider`
        （规则层照常可用，需要模型时降级，不返回假结果）。

    Raises:
        ValueError: 未知 provider 名称。
        RuntimeError: （仅 ``FakeLlmProvider(is_production=True)`` 直接构造时）
    """
    kind = (getattr(settings, "llm_provider", "") or "fake").strip().lower()
    if kind == "fake":
        if bool(getattr(settings, "is_production", lambda: False)()):
            # 生产不装配 FakeLlmProvider（它会返回假结果），但**不能构造期抛**——
            # 规则层命中的请求不需要模型。改装配 UnavailableLlmProvider：调用才抛，
            # 由 LlmFallback 降级为澄清/手动录入。
            provider: LlmProvider = UnavailableLlmProvider()
        else:
            provider = FakeLlmProvider(
                fixtures,
                default=default,
                is_production=False,
            )
    elif kind == "real":
        provider = RealLlmProvider(
            api_key=getattr(settings, "llm_api_key", ""),
            base_url=getattr(settings, "llm_base_url", ""),
            model=getattr(settings, "llm_model", ""),
            timeout_seconds=float(getattr(settings, "llm_timeout_seconds", 6.0)),
            max_tokens=int(getattr(settings, "llm_max_tokens", 1024)),
        )
    else:
        raise ValueError(f"未知 LLM_PROVIDER={kind!r}（只支持 fake / real）")
    return CountingLlmProvider(provider) if wrap_counter else provider
