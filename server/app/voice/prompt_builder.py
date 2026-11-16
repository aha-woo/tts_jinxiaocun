"""prompt 构造（G4 的输入端）：**注入预算** + 静态/动态分离 + 确定性输出。

三条纪律（架构 §4 ④）
----------------------
1. **注入预算**：商品 Top N（默认 20）+ 最近 3 条上下文，**绝不注入全量档案**
   （全量档案既贵又会把模型带偏，还容易泄露跨租户数据）；
2. **静态/动态分离**：契约说明、intent 摘要、few-shot、Top 商品属于**静态前缀**
   （走 :mod:`app.voice.prompt_cache`），只有「用户原话 + 最近上下文」是动态段；
3. **确定性**：同样的输入必须产出**逐字符相同**的 prompt —— 否则 prefix 缓存永远不命中，
   而且回归测试无法比对。

本模块不调模型、不查库：素材由调用方（T04 服务层）按租户装配好后传进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.domain.intent_registry import IntentRegistry, default_registry
from app.voice.alias_resolver import ProductEntry
from app.voice.output_schema import SOURCE_VALUES
from app.voice.prompt_cache import prefix_key

__all__ = [
    "DEFAULT_RECENT_CONTEXTS",
    "DEFAULT_TOP_PRODUCTS",
    "PromptBudget",
    "PromptContext",
    "build_prompt",
    "build_static_prefix",
    "build_user_block",
    "static_prefix_cache_key",
]

#: 注入预算默认值（需核实：越大越贵，20 是成本与召回的经验折中）
DEFAULT_TOP_PRODUCTS = 20
DEFAULT_RECENT_CONTEXTS = 3
#: few-shot 条数上限
DEFAULT_FEW_SHOT = 8


@dataclass(frozen=True)
class PromptBudget:
    """注入预算（可配置，**代码里不许出现魔法数字**）。"""

    top_products: int = DEFAULT_TOP_PRODUCTS
    recent_contexts: int = DEFAULT_RECENT_CONTEXTS
    few_shot: int = DEFAULT_FEW_SHOT

    def clipped_products(self, count: int) -> int:
        """商品条数裁剪。"""
        return max(0, min(count, max(0, self.top_products)))

    def clipped_contexts(self, count: int) -> int:
        """上下文条数裁剪。"""
        return max(0, min(count, max(0, self.recent_contexts)))


@dataclass
class PromptContext:
    """构造 prompt 所需的全部素材（按租户装配，本模块不做 IO）。"""

    user_text: str
    today_iso: str
    store_name: str = "主仓"
    tenant_id: str = "-"
    contract_version: str = "0"
    domain_hint: str | None = None
    #: 商品 TopN（已按频次降序）
    products: Sequence[ProductEntry] = ()
    #: 个人词表 ``{说法: 正名}``
    personal_aliases: Mapping[str, str] = field(default_factory=dict)
    #: 最近上下文（每条是一句已确认的摘要，最多 3 条）
    recent_contexts: Sequence[str] = ()
    budget: PromptBudget = field(default_factory=PromptBudget)
    #: 门店内多门店名（帮助模型别猜门店）
    store_names: Sequence[str] = ()

    def products_block(self) -> str:
        """Top N 商品段（确定性排序：先按传入顺序，再按名字）。"""
        selected = list(self.products)[: self.budget.clipped_products(len(self.products))]
        if not selected:
            return "（无）"
        lines = [
            "- {}（基本单位 {}）".format(entry.display_name() or entry.name, entry.base_unit)
            for entry in selected
        ]
        return "\n".join(lines)

    def aliases_block(self) -> str:
        """个人词表段（教说法回流；命中时 source=alias）。"""
        if not self.personal_aliases:
            return "（无）"
        items = sorted(self.personal_aliases.items())
        return "\n".join(f"- {term} = {name}" for term, name in items)

    def contexts_block(self) -> str:
        """最近上下文段（最多 3 条，防止越长越贵）。"""
        selected = list(self.recent_contexts)[: self.budget.clipped_contexts(len(self.recent_contexts))]
        if not selected:
            return "（无）"
        return "\n".join(f"- {item}" for item in selected)


def _intent_digest(registry: IntentRegistry, domain: str | None, limit: int) -> str:
    """intent 摘要段（只放 id + 中文名 + 必填槽位，不放大段描述）。"""
    specs = registry.few_shot_examples(domain=domain, limit=limit)
    if not specs:
        specs = registry.few_shot_examples(limit=limit)
    lines = [
        "- {}｜{}｜必填 {}｜风险 {}/{}".format(
            spec.intent_id,
            spec.name_zh,
            ",".join(spec.required_slots) or "无",
            spec.risk_level,
            spec.confirm_policy,
        )
        for spec in specs
    ]
    return "\n".join(lines) if lines else "（无）"


def _few_shot_block(registry: IntentRegistry, domain: str | None, limit: int) -> str:
    """few-shot 段：每个意图一条最典型原话（**不是全部样例**）。"""
    specs = registry.few_shot_examples(domain=domain, limit=limit)
    lines: list[str] = []
    for spec in specs:
        if not spec.sample_utterances:
            continue
        lines.append(f'"{spec.sample_utterances[0]}" -> {{"intent": "{spec.intent_id}"}}')
    return "\n".join(lines) if lines else "（无）"


def build_static_prefix(
    ctx: PromptContext,
    *,
    registry: IntentRegistry | None = None,
) -> str:
    """静态前缀（可缓存）：契约版本 + 输出约束 + intent 摘要 + few-shot + 商品 + 词表。"""
    reg = registry or default_registry()
    sources = "|".join(SOURCE_VALUES)
    return "\n".join(
        (
            "你是「语音录入版轻量化进销存」的意图解析器。",
            f"契约版本：{ctx.contract_version}",
            f"今天是：{ctx.today_iso}（业务日，租户时区 Asia/Shanghai）",
            f"当前门店：{ctx.store_name}",
            "只输出 JSON 对象，字段严格如下：",
            '{"intent": string, "slots": {code: value}, "source": {code: "%s"}, '
            '"confidence": 0~1, "clarify": null | {"slot","question","options":[2~3 项]}, '
            '"calc_traces": [string]}' % sources,
            "硬规则：",
            "1) 金额一律整数分（如 35 元 = 3500），禁止小数；",
            "2) slots 与 source 的键必须完全一致，source 只能取上面的枚举；",
            "3) 单位换算系数只允许来自档案，取不到就填 RATIO 缺失并走 pending，禁止臆造；",
            "4) 「还剩 N」是盘点快照，「再入 N」是增量入库，两者绝不可混；",
            "5) 分不清单价还是总价时必须 clarify，不许猜；",
            "6) 日期必须写成 YYYY-MM-DD，不许写「昨天」；",
            "7) 盘点/退货/金额类白名单动作只能生成待办，不许直接写库。",
            "",
            "[可解析意图（id｜中文名｜必填槽位｜风险/确认）]",
            _intent_digest(reg, ctx.domain_hint, ctx.budget.few_shot),
            "",
            "[标准句式示例]",
            _few_shot_block(reg, ctx.domain_hint, ctx.budget.few_shot),
            "",
            f"[本店商品 Top{ctx.budget.top_products}（只允许从中选 P，不在表里就按新增候选）]",
            ctx.products_block(),
            "",
            "[老板的个人叫法（命中时 source 用 alias）]",
            ctx.aliases_block(),
            "",
            f"[最近 {ctx.budget.recent_contexts} 条上下文（仅辅助判断，不要照抄）]",
            ctx.contexts_block(),
            "",
            "[门店清单（未指明就 clarify，禁止跨店猜测）]",
            "|".join(ctx.store_names) if ctx.store_names else "（单门店）",
        )
    )


def build_user_block(ctx: PromptContext) -> str:
    """动态段：只说「用户这次说了什么」（越短越省）。"""
    return f"[用户原话]\n{ctx.user_text.strip()}"


def build_prompt(
    ctx: PromptContext,
    *,
    registry: IntentRegistry | None = None,
) -> str:
    """拼完整 prompt：静态前缀 + 动态原话（顺序固定，保证前缀缓存可命中）。"""
    return build_static_prefix(ctx, registry=registry) + "\n\n" + build_user_block(ctx)


def static_prefix_cache_key(ctx: PromptContext, *, registry: IntentRegistry | None = None) -> str:
    """静态前缀的缓存 key（与 :func:`build_static_prefix` 的输出一一对应）。"""
    reg = registry or default_registry()
    products = "|".join(
        entry.display_name() or entry.name
        for entry in list(ctx.products)[: ctx.budget.clipped_products(len(ctx.products))]
    )
    return prefix_key(
        tenant_id=ctx.tenant_id,
        contract_version=f"{ctx.contract_version}:{reg.version}",
        domain=ctx.domain_hint,
        products=products,
    )


def summarize_contexts(items: Iterable[Any]) -> tuple[str, ...]:
    """把上下文对象压成可注入的短句（多态容错：dict / 对象 / 字符串都行）。"""
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            text = str(
                item.get("summary")
                or item.get("text")
                or item.get("intent")
                or item.get("intent_id")
                or ""
            )
        else:
            text = str(
                getattr(item, "summary", None)
                or getattr(item, "intent_id", None)
                or getattr(item, "text", None)
                or ""
            )
        cleaned = text.strip()
        if cleaned:
            result.append(cleaned[:60])
    return tuple(result[:DEFAULT_RECENT_CONTEXTS])
