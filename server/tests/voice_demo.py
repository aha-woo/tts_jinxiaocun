"""T03 语音链路的共享测试夹具（**全部离线**，零网络、零数据库）。

这里只做两件事：
1. 造一份贴近真实小店的商品/往来单位词表（可乐、娃哈哈、红牛、矿泉水 + 常见客户/供应商）；
2. 提供装配好的 ``VoicePipeline``，其中 LLM 一定是
   ``CountingLlmProvider(FakeLlmProvider())`` —— 这样「有没有调模型」是**可数的**，
   而不是靠人工检查。

**注意**：本文件的词表是测试夹具，**不是商品主数据**；生产由 T04 从
``product`` / ``product_alias`` 表装配（见 ``alias_resolver.ProductEntry`` 注释）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.domain.intent_registry import IntentRegistry, default_registry
from app.voice.alias_resolver import AliasResolver, ProductEntry
from app.voice.clarify_fsm import ClarifyFSM
from app.voice.gate import Gate
from app.voice.llm_fallback import LlmFallback
from app.voice.llm_provider import CountingLlmProvider, FakeLlmProvider
from app.voice.non_command_filter import NonCommandFilter
from app.voice.pipeline import VoicePipeline
from app.voice.prompt_cache import PromptCache
from app.voice.template_engine import TemplateEngine

#: 固定业务日（避免测试随真实日期漂移）
TODAY = "2025-11-15"


def demo_products() -> list[ProductEntry]:
    """演示商品档案（4 个 SKU，含别名与换算系数）。"""
    return [
        ProductEntry(
            product_id="11111111-1111-1111-1111-111111111111",
            name="可乐",
            base_unit="瓶",
            spec="500ml",
            aliases=("快乐水", "大瓶可乐"),
            last_purchase_price_fen=180,
            last_sale_price_fen=300,
            ratios={"箱": 24, "件": 24},
        ),
        ProductEntry(
            product_id="22222222-2222-2222-2222-222222222222",
            name="娃哈哈",
            base_unit="瓶",
            spec="596ml",
            last_purchase_price_fen=90,
            last_sale_price_fen=150,
            ratios={"箱": 24, "件": 24},
        ),
        ProductEntry(
            product_id="33333333-3333-3333-3333-333333333333",
            name="红牛",
            base_unit="罐",
            spec="250ml",
            last_purchase_price_fen=450,
            last_sale_price_fen=600,
            ratios={"箱": 24},
        ),
        ProductEntry(
            product_id="44444444-4444-4444-4444-444444444444",
            name="矿泉水",
            base_unit="瓶",
            spec="550ml",
            last_purchase_price_fen=80,
            last_sale_price_fen=150,
            # 故意**不给**「箱」的换算系数：验证「缺系数不臆造」
        ),
    ]


def demo_partners() -> list[ProductEntry]:
    """演示往来单位（客户 + 供应商共用一份词表，生产上分开）。"""
    return [
        ProductEntry(product_id="c0000001-0000-0000-0000-000000000001", name="李婶"),
        ProductEntry(product_id="c0000002-0000-0000-0000-000000000002", name="兴旺超市"),
        ProductEntry(product_id="c0000003-0000-0000-0000-000000000003", name="二中店"),
        ProductEntry(product_id="c0000004-0000-0000-0000-000000000004", name="来福超市"),
        ProductEntry(product_id="s0000001-0000-0000-0000-000000000001", name="老张"),
        ProductEntry(product_id="s0000002-0000-0000-0000-000000000002", name="王老板"),
        ProductEntry(product_id="s0000003-0000-0000-0000-000000000003", name="南门批发"),
    ]


def demo_resolver() -> AliasResolver:
    """商品词表解析器。"""
    return AliasResolver(demo_products())


def demo_partners_resolver() -> AliasResolver:
    """往来单位词表解析器（客户 + 供应商**合并**一份，仅用于认名字）。"""
    return AliasResolver(demo_partners())


def demo_customers_resolver() -> AliasResolver:
    """**只含客户**的词表（方向判据 ``partner.type`` 用；生产按 ``partner.type`` 装）。

    演示数据的 ``partner_id`` 前缀就是类型（``c…`` = 客户、``s…`` = 供应商），
    与生产 ``partner.type ∈ {customer, supplier}`` 一一对应。
    """
    return AliasResolver([row for row in demo_partners() if row.product_id.startswith("c")])


def demo_suppliers_resolver() -> AliasResolver:
    """**只含供应商**的词表（同上）。"""
    return AliasResolver([row for row in demo_partners() if row.product_id.startswith("s")])


def build_pipeline(
    *,
    fixtures: Mapping[str, Mapping[str, Any] | str] | None = None,
    default: Mapping[str, Any] | None = None,
    registry: IntentRegistry | None = None,
    store_name: str = "主仓",
    store_names: tuple[str, ...] = (),
) -> tuple[VoicePipeline, CountingLlmProvider]:
    """装配一条**完全离线**的流水线，返回 ``(pipeline, llm_counter)``。"""
    reg = registry or default_registry()
    fake = FakeLlmProvider(fixtures, default=default, is_production=False)
    counter = CountingLlmProvider(fake)
    cache = PromptCache(max_entries=64, ttl_seconds=600)
    pipeline = VoicePipeline(
        engine=TemplateEngine(
            registry=reg,
            store_name=store_name,
            store_names=store_names,
        ),
        fallback=LlmFallback(counter, cache=cache, max_retries=1, registry=reg),
        non_command_filter=NonCommandFilter(),
        gate=Gate(),
        clarify=ClarifyFSM(registry=reg),
        registry=reg,
        provider=counter,
        contract_version=reg.version,
        store_name=store_name,
        store_names=store_names,
    )
    return pipeline, counter


def sample_regression_set() -> list[tuple[str, bool, str]]:
    """从 ``intents.json`` 读全量 ``sample_utterances`` 当回归集。

    Returns:
        ``[(intent_id, template_hit, utterance), ...]`` —— 顺序稳定（按 ``seq``）。
    """
    path = Path(__file__).resolve().parents[2] / "packages" / "shared" / "contracts" / "intents.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[str, bool, str]] = []
    for intent in doc["intents"]:
        for utterance in intent.get("sample_utterances", []):
            rows.append((intent["intent_id"], bool(intent.get("template_hit")), utterance))
    return rows


def run_utterance(pipeline: VoicePipeline, text: str) -> Any:
    """用统一参数跑一句（固定业务日 + 演示词表）。"""
    return pipeline.run(
        text,
        today_iso=TODAY,
        resolver=demo_resolver(),
        customers=demo_partners_resolver(),
        suppliers=demo_partners_resolver(),
        session_key="pytest",
    )


def fresh_pipeline() -> Iterator[VoicePipeline]:
    """pytest fixture 用：每条用例一条干净流水线（避免 60s 去重互相干扰）。"""
    pipeline, _counter = build_pipeline()
    yield pipeline
