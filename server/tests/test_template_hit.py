"""验收③：**规则层命中率实测** + 18 个 ``template_hit`` 意图 **0 次 LLM 调用**。

这个文件回答两个必须用数字回答的问题
------------------------------------
1. **G3 命中率到底是多少？**
   回归集 = ``intents.json`` 的全部 ``sample_utterances``（84 条），
   一句一条跑完整流水线，统计「停在 G3（``rule_hit=True``）」的比例。
   PRD 的目标是 ≥45%，**实测多少就报多少**；本文件把实测值写成常量基线，
   只允许**向上**突破（``>=``），不允许为了好看去下调阈值或改样本。

2. **18 个 ``template_hit`` 意图是不是真的 100% 不调 LLM？**
   不是靠 review，靠计数：provider 是 ``CountingLlmProvider(FakeLlmProvider())``，
   逐个意图断言 ``counter.calls == 0``。规则层漏一个，这条断言立刻红。

另外两条「反向」断言同样重要
---------------------------
- **非模板意图不得被规则层硬猜**：``template_hit=false`` 的样本必须升级到 G4
  （否则等于用规则瞎猜 R2/R3 风险动作，比多花一次模型钱危险得多）；
- **多商品与单商品不能互相抢**：``*_multi`` 只在「一句话 ≥2 个商品」时命中。
"""

from __future__ import annotations

import pytest

from app.domain.intent_registry import default_registry
from voice_demo import (
    TODAY,
    build_pipeline,
    demo_partners_resolver,
    demo_resolver,
    sample_regression_set,
)

#: 回归集规模（= intents.json 的 sample_utterances 总数，契约变更时会一起变）
EXPECTED_SAMPLES = 84

#: 18 个 ``template_hit=true`` 的意图（契约快照；变更须改文档）
TEMPLATE_HIT_INTENTS = (
    "purchase.create",
    "purchase.create_multi",
    "sales.create",
    "sales.create_multi",
    "stock.take",
    "stock.take_multi",
    "stock.loss",
    "stock.query",
    "doc.query",
    "doc.query_summary",
    "debt.query",
    "product.create",
    "product.alias.add",
    "report.today",
    "sys.cancel",
    "sys.continue",
    "sys.help",
    "sys.example",
)

#: **实测基线**（2026-xx 在本仓库内跑出的真实数字，只许往上走）
BASELINE_RULE_HITS = 42
BASELINE_TEMPLATE_HIT_COVERED = 42


@pytest.fixture()
def regression_rows():
    """回归集（按契约 seq 顺序）。"""
    rows = sample_regression_set()
    assert len(rows) == EXPECTED_SAMPLES, f"回归集规模变了：{len(rows)}"
    return rows


def test_template_hit_intents_are_exactly_the_contract_ones() -> None:
    """18 个模板意图必须与契约 ``template_hit=true`` 完全一致（防口径漂移）。"""
    registry = default_registry()
    assert registry.template_hit_ids() == frozenset(TEMPLATE_HIT_INTENTS)
    assert len(registry.template_hit_ids()) == 18


@pytest.mark.parametrize("intent_id", TEMPLATE_HIT_INTENTS)
def test_each_template_hit_intent_calls_llm_zero_times(intent_id: str) -> None:
    """**验收③ 的核心断言**：每个模板意图跑完，provider 的调用次数必须是 0。"""
    pipeline, counter = build_pipeline()
    registry = default_registry()
    spec = registry.require(intent_id)
    assert spec.template_hit is True

    for utterance in spec.sample_utterances:
        result = pipeline.run(
            utterance,
            today_iso=TODAY,
            resolver=demo_resolver(),
            customers=demo_partners_resolver(),
            suppliers=demo_partners_resolver(),
            session_key=f"tmpl-{intent_id}",
        )
        # 1) 必须由规则层（或 G2 静态路由）给出结论，而不是升级到 G4
        assert result.stage in {"G2", "G3"}, (
            f"{intent_id} 的「{utterance}」跑到了 {result.stage}（{result.reason}），"
            "说明规则层漏了这个标准句式"
        )
        assert result.rule_hit is True or result.stage == "G2"
        # 2) 一次模型都不能调
        assert counter.calls == 0, (
            f"{intent_id} 的「{utterance}」调了 {counter.calls} 次模型；"
            "模板意图必须 100% 走规则层"
        )
        # 3) 解析结果必须过同一个 Schema
        assert result.parse is not None
        assert result.parse.channel in {"template", None}


def test_rule_hit_rate_on_full_regression_set(regression_rows) -> None:
    """全量回归集的实际命中率（**把数字打出来，不只断言**）。"""
    pipeline, counter = build_pipeline()
    hits = 0
    template_rows = 0
    template_covered = 0
    escalated = 0
    surprises: list[str] = []

    for intent_id, template_hit, utterance in regression_rows:
        result = pipeline.run(
            utterance,
            today_iso=TODAY,
            resolver=demo_resolver(),
            customers=demo_partners_resolver(),
            suppliers=demo_partners_resolver(),
            session_key="rate",
        )
        if template_hit:
            template_rows += 1
            if result.rule_hit or result.stage == "G2":
                template_covered += 1
            else:
                surprises.append(f"{intent_id}: {utterance} -> {result.stage}/{result.reason}")
        if result.llm_calls > 0:
            escalated += 1
        if result.rule_hit or result.stage == "G2":
            hits += 1

    total = len(regression_rows)
    rate = hits / total
    print(
        f"\n[G3 实测] 回归集 {total} 条，规则层命中 {hits} 条 = {rate:.1%}；"
        f"其中 template_hit 样本 {template_rows} 条，命中 {template_covered} 条 = "
        f"{template_covered / template_rows:.1%}；升级到 G4 的样本 {escalated} 条，"
        f"模型调用 {counter.calls} 次（含 Schema 修正重试）"
    )
    if surprises:  # pragma: no cover - 回归失败时才打印
        print("未命中明细：\n" + "\n".join(surprises))

    # PRD 假设目标 ≥45%
    assert rate >= 0.45, f"规则层命中率 {rate:.1%} 低于 PRD 假设的 45%"
    # 实测基线（只许往上走，不许为了好看改这里）
    assert hits >= BASELINE_RULE_HITS, f"命中数从基线 {BASELINE_RULE_HITS} 掉到 {hits}"
    # 18 个模板意图的标准句式必须 100% 被规则层接住
    assert template_covered == BASELINE_TEMPLATE_HIT_COVERED, surprises


def test_non_template_intents_are_escalated_not_guessed(regression_rows) -> None:
    """``template_hit=false`` 的样本**不得**被规则层硬猜（宁可花钱问模型）。"""
    pipeline, _counter = build_pipeline()
    false_positives: list[str] = []
    for intent_id, template_hit, utterance in regression_rows:
        if template_hit:
            continue
        result = pipeline.run(
            utterance,
            today_iso=TODAY,
            resolver=demo_resolver(),
            customers=demo_partners_resolver(),
            suppliers=demo_partners_resolver(),
            session_key="fp",
        )
        if result.rule_hit:
            false_positives.append(f"{intent_id}: {utterance} -> {result.intent_id}")
    assert false_positives == [], "非模板意图被规则层猜中：\n" + "\n".join(false_positives)


def test_multi_intent_only_fires_for_multiple_products() -> None:
    """``*_multi`` 只在「一句话 ≥2 个商品」时命中，不得抢走单商品句。"""
    pipeline, counter = build_pipeline()

    single = pipeline.run(
        "今天进了两箱可乐，一件三十五",
        today_iso=TODAY,
        resolver=demo_resolver(),
        session_key="multi-single",
    )
    assert single.intent_id == "purchase.create"

    multi = pipeline.run(
        "进点货：红牛一件、和其正两件，都按老价",
        today_iso=TODAY,
        resolver=demo_resolver(),
        session_key="multi-multi",
    )
    assert multi.intent_id == "purchase.create_multi"
    assert len([item for item in multi.template.items if item.product_name]) >= 2
    assert counter.calls == 0


def test_static_intents_never_reach_the_model() -> None:
    """``sys.help`` / ``sys.example`` 是本地静态内容：既不进 G3 也不调 LLM。"""
    pipeline, counter = build_pipeline()
    for utterance in ("帮助", "你能听懂啥", "举个例子", "说一句我听听"):
        result = pipeline.run(utterance, today_iso=TODAY, session_key="static")
        assert result.stage == "G2"
        assert result.route == "static"
        assert result.static_answer
    assert counter.calls == 0


def test_prompt_cache_hit_avoids_second_llm_call() -> None:
    """同一句话第二次解析命中结果缓存：**一次模型都不再调**。

    这里直接打 ``LlmFallback``：流水线层的 60s 文本去重会先一步拦下重复话术
    （那是 G2 的职责），缓存这层要单独验证才有意义。
    """
    from app.voice.llm_fallback import LlmFallback
    from app.voice.llm_provider import CountingLlmProvider, FakeLlmProvider
    from app.voice.prompt_builder import PromptContext
    from app.voice.prompt_cache import PromptCache

    counter = CountingLlmProvider(
        FakeLlmProvider(
            default={
                "intent": "partner.create",
                "slots": {"TF": "名称", "TV": "兴旺超市"},
                "source": {"TF": "asr", "TV": "asr"},
                "confidence": 0.9,
            }
        )
    )
    cache = PromptCache(max_entries=8, ttl_seconds=600)
    fallback = LlmFallback(counter, cache=cache, max_retries=1)
    ctx = PromptContext(
        user_text="新客户，兴旺超市，联系人刘姐",
        today_iso=TODAY,
        tenant_id="tenant-demo",
        contract_version="1.1.0",
        domain_hint="partner",
    )

    first = fallback.parse(ctx)
    assert first.ok is True, first.trace
    assert counter.calls == 1, "第一次应真的调一次模型"

    second = fallback.parse(ctx)
    assert counter.calls == 1, "第二次应命中结果缓存，不得再调模型"
    assert second.cache_hit is True
    assert second.result is not None and second.result.intent == "partner.create"
