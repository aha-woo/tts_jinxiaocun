"""成本护栏的内部单测：``cost_monitor`` / ``degrade`` / ``prompt_builder`` / provider 误用。

本轮（T03）不接 API，但这几个数字必须是**真实算出来的**，否则 T04 一接就露馅：

1. ``cost_monitor``：规则层命中率、LLM 调用率、prompt 缓存命中率、平均 tokens、单次成本；
2. ``degrade``：L0→L4 逐级关能力，**降级不停止服务**，历史可查可导出；
3. ``prompt_builder``：注入预算（Top20 商品 / 最近 3 条上下文）**真的会裁剪**，
   且同样输入产出**逐字符相同**的 prompt（否则前缀缓存永远不命中）；
4. ``llm_provider``：实时误用要**立刻炸**（fake 进生产 / real 缺凭证）。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.cost_monitor import CostMonitor, CostRates
from app.services.degrade import DEGRADE_LEVELS, DegradeController, DegradePolicy
from app.voice.alias_resolver import ProductEntry
from app.voice.llm_provider import (
    CountingLlmProvider,
    FakeLlmProvider,
    LlmProviderError,
    RealLlmProvider,
    UnavailableLlmProvider,
    build_llm_provider,
)
from app.voice.prompt_builder import (
    DEFAULT_RECENT_CONTEXTS,
    DEFAULT_TOP_PRODUCTS,
    PromptBudget,
    PromptContext,
    build_prompt,
    build_static_prefix,
    summarize_contexts,
)
from app.voice.prompt_cache import PromptCache


# --------------------------------------------------------------------------- 成本
def test_cost_monitor_reports_the_five_required_numbers() -> None:
    """五个指标必须齐：命中率 / LLM 调用率 / 缓存命中率 / 平均 tokens / 单次成本。"""
    cache = PromptCache(max_entries=4, ttl_seconds=60)
    cache.set("k", "v")
    cache.get("k")
    cache.get("miss")

    monitor = CostMonitor(rates=CostRates(input_fen_per_1k=0.2, output_fen_per_1k=0.8), prompt_cache=cache)
    # 4 次请求：3 次规则层命中，1 次走 LLM（2 次调用，含一次重试）
    for _ in range(3):
        monitor.record(stage="G3", rule_hit=True)
    monitor.record(
        stage="G4", rule_hit=False, llm_calls=2, tokens_in=2000, tokens_out=400
    )

    snapshot = monitor.snapshot()
    assert snapshot.total_requests == 4
    assert snapshot.rule_hit_rate == 0.75
    assert snapshot.llm_call_rate == 0.25
    assert snapshot.llm_calls == 2
    assert snapshot.avg_tokens_in == 1000.0
    assert snapshot.avg_tokens_out == 200.0
    assert snapshot.prompt_cache_hit_rate == 0.5
    # 2000/1000*0.2 + 400/1000*0.8 = 0.4 + 0.32 = 0.72 分 → 向上取整 1 分
    assert snapshot.cost_fen == 1
    assert snapshot.cost_per_request_fen == 0.25
    assert set(snapshot.as_dict()) >= {
        "rule_hit_rate",
        "llm_call_rate",
        "prompt_cache_hit_rate",
        "avg_tokens_in",
        "cost_per_request_fen",
    }


def test_rule_hits_cost_nothing() -> None:
    """规则层命中 = 0 成本（这是「省钱」两个字的全部含义）。"""
    monitor = CostMonitor()
    for _ in range(10):
        monitor.record(stage="G3", rule_hit=True)
    snapshot = monitor.snapshot()
    assert snapshot.cost_fen == 0
    assert snapshot.llm_calls == 0
    assert snapshot.rule_hit_rate == 1.0


def test_gate_rejections_are_not_counted_in_hit_rate() -> None:
    """误触（G0）不计入分母，否则命中率会被手抖连点稀释。"""
    monitor = CostMonitor()
    monitor.record(stage="G3", rule_hit=True)
    monitor.record(stage="G0", error_code="E_GATE_REJECT", counted=False)
    assert monitor.snapshot().total_requests == 1
    assert monitor.snapshot().rule_hit_rate == 1.0
    assert monitor.snapshot().by_stage["G0"] == 1


def test_cost_ratio_feeds_degrade() -> None:
    """成本占预算比例可直接喂给降级判定。"""
    monitor = CostMonitor(rates=CostRates(input_fen_per_1k=1000.0, output_fen_per_1k=0.0))
    monitor.record(stage="G4", llm_calls=1, tokens_in=1000, tokens_out=0)
    assert monitor.snapshot().cost_fen == 1000
    assert monitor.cost_ratio(budget_fen=500) == 2.0


def test_monitor_accepts_pipeline_result_shape() -> None:
    """``record_pipeline`` 能吃流水线结果（T04 唯一调用点）。"""

    class FakeResult:
        stage = "G3"
        rule_hit = True
        llm_calls = 0
        llm = None
        error_code = None
        degraded = False

    monitor = CostMonitor()
    monitor.record_pipeline(FakeResult())
    assert monitor.snapshot().rule_hit_rate == 1.0


# --------------------------------------------------------------------------- 降级
def test_degrade_levels_turn_off_the_expensive_parts_only() -> None:
    """逐级降级：L2 关 LLM、L3 关语音解析、L4 只读 —— 但**永远保留查账**。"""
    controller = DegradeController()
    assert controller.level == "L0"
    assert controller.allows("llm_fallback") is True

    controller.decide(usage_ratio=0.95)
    assert controller.level == "L2"
    assert controller.allows("llm_fallback") is False
    assert controller.allows("voice_parse") is True, "L2 仍要能录高频句式"

    controller.decide(usage_ratio=1.0)
    assert controller.level == "L3"
    assert controller.allows("voice_parse") is False
    assert controller.allows("write_ops") is True, "L3 仍要能手动录单"

    controller.decide(usage_ratio=1.0, cost_ratio=5.0)
    assert controller.level == "L3", "已到 L3 不会再自动跳到 L4（L4 需显式开启）"


def test_degrade_only_gets_stricter_until_manual_reset() -> None:
    """只升不降：避免额度在阈值附近抖动导致体验忽好忽坏。"""
    controller = DegradeController()
    controller.decide(usage_ratio=1.0)
    assert controller.level == "L3"
    controller.decide(usage_ratio=0.0)
    assert controller.level == "L3", "自动恢复会造成抖动，恢复必须人工 reset"

    controller.reset(level="L0", reason="运营补偿")
    assert controller.level == "L0"


def test_degrade_history_is_queryable_and_exportable() -> None:
    """降级历史**可查可导出**（复盘「什么时候为什么降的」）。"""
    controller = DegradeController()
    controller.decide(usage_ratio=0.95)
    controller.decide(usage_ratio=0.1, error_rate=0.5)

    history = controller.history()
    assert len(history) >= 2
    assert history[0]["level"] in DEGRADE_LEVELS
    assert all("reasons" in item and "features" in item for item in history)
    assert len(controller.export()) == len(controller.history(limit=999))


def test_force_l4_is_read_only() -> None:
    """``force_l4`` → 写操作全关（只读模式）。"""
    controller = DegradeController(policy=DegradePolicy(force_l4=True))
    controller.decide(usage_ratio=0.0)
    assert controller.level == "L4"
    assert controller.allows("write_ops") is False
    assert controller.allows("voice_capture") is False


# --------------------------------------------------------------------------- 注入预算
def _products(count: int) -> tuple[ProductEntry, ...]:
    """造 N 个商品（用于验证裁剪）。"""
    return tuple(ProductEntry(product_id=f"p{i}", name=f"商品{i:02d}") for i in range(count))


def test_prompt_budget_clips_products_and_contexts() -> None:
    """注入预算真的会裁剪：商品 Top20、上下文 3 条。"""
    ctx = PromptContext(
        user_text="今天进了两箱可乐",
        today_iso="2025-11-15",
        products=_products(50),
        recent_contexts=tuple(f"上下文{i}" for i in range(10)),
        budget=PromptBudget(top_products=DEFAULT_TOP_PRODUCTS, recent_contexts=DEFAULT_RECENT_CONTEXTS),
    )
    products_block = ctx.products_block()
    assert products_block.count("- ") == DEFAULT_TOP_PRODUCTS
    assert "商品20" not in products_block, "第 21 个商品不应被注入（省 token）"
    assert ctx.contexts_block().count("- ") == DEFAULT_RECENT_CONTEXTS


def test_prompt_is_byte_identical_for_same_input() -> None:
    """同样的输入必须产出**逐字符相同**的 prompt（前缀缓存的前提）。"""
    ctx = PromptContext(user_text="盘点，可乐还剩 8 箱", today_iso="2025-11-15", products=_products(3))
    assert build_prompt(ctx) == build_prompt(ctx)
    assert build_static_prefix(ctx) == build_static_prefix(ctx)


def test_prompt_forbids_guessing_in_writing() -> None:
    """prompt 里必须写死「不许猜」的硬规则（金额整数分、快照 vs 增量、澄清）。"""
    ctx = PromptContext(user_text="可乐 35", today_iso="2025-11-15")
    prefix = build_static_prefix(ctx)
    for rule in ("整数分", "还剩", "再入", "clarify", "YYYY-MM-DD", "待办"):
        assert rule in prefix, f"prompt 缺少硬规则：{rule}"


def test_summarize_contexts_truncates_to_budget() -> None:
    """上下文摘要最多 3 条且逐条截断（防止历史越长越贵）。"""
    items = [{"summary": "第 %d 条" % index} for index in range(6)]
    result = summarize_contexts(items)
    assert len(result) == DEFAULT_RECENT_CONTEXTS
    assert all(len(item) <= 60 for item in result)


# --------------------------------------------------------------------------- provider 误用
def test_fake_provider_never_used_in_production() -> None:
    """生产**绝不装配** ``FakeLlmProvider``，但也**不在构造期炸**。

    2026-11-19 线上 500 的根因：`APP_ENV=prod` + 未配 LLM 时，装配运行时直接
    `RuntimeError: FakeLlmProvider 禁止在生产环境使用`，把「进货可乐」这种
    **只走规则层**的说法也一起打死了。

    正确语义是：构造期给一个 ``UnavailableLlmProvider``（规则层照常工作），
    真需要模型时才抛 ``LlmProviderError`` → 由 `LlmFallback` 降级成澄清/手动。
    它**不返回任何假结果**，所以安全性与原来一致。
    """
    # 类本身的护栏保留：直接构造仍然立刻炸
    with pytest.raises(RuntimeError):
        FakeLlmProvider(is_production=True)

    provider = build_llm_provider(Settings(env="production", llm_provider="fake"))
    assert isinstance(provider, CountingLlmProvider)
    assert isinstance(provider.inner, UnavailableLlmProvider)
    with pytest.raises(LlmProviderError):
        provider.parse("随便一句", {})


def test_real_provider_requires_credentials_at_construction() -> None:
    """``RealLlmProvider`` 缺凭证在**构造期**就炸，不等第一次调用。"""
    with pytest.raises(ValueError) as excinfo:
        RealLlmProvider(api_key="", base_url="", model="")
    message = str(excinfo.value)
    assert "LLM_API_KEY" in message and "LLM_BASE_URL" in message
    with pytest.raises(ValueError):
        build_llm_provider(Settings(llm_provider="real"))


def test_unknown_provider_name_is_rejected() -> None:
    """未知 provider 名称直接报错（不静默回落到 fake）。"""
    with pytest.raises(ValueError):
        build_llm_provider(Settings(llm_provider="openai-like-typo"))


def test_counting_provider_counts_calls_and_tokens() -> None:
    """计数包装器：调用数与 token 都能被断言（验收③的证据来源）。"""
    counter = CountingLlmProvider(FakeLlmProvider(default={"intent": "stock.query", "slots": {}, "source": {}, "confidence": 0.9}))
    assert counter.calls == 0
    counter.parse("prompt", {})
    counter.parse("prompt", {})
    assert counter.calls == 2
    assert counter.tokens_in > 0 and counter.tokens_out > 0
    assert counter.stats.snapshot()["calls"] == 2
    counter.reset()
    assert counter.calls == 0


def test_default_settings_are_fake_and_never_touch_network() -> None:
    """默认配置必须是 fake：本地/CI 跑测试不会误连现网烧钱。"""
    settings = Settings()
    assert settings.llm_provider == "fake"
    assert settings.asr_provider == "fake"
    assert settings.llm_is_fake() is True
    assert settings.asr_is_fake() is True
    assert isinstance(build_llm_provider(settings), CountingLlmProvider)
