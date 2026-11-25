"""语音运行时装配：**DB 词表 → 解析流水线 → 卡片明细/预演 delta**。

为什么要有这一层
---------------
路由层不该自己拼 ``TemplateEngine`` / ``LlmFallback`` / ``PromptCache``——
拼错一个参数（比如忘了注入词表）就会变成「规则层永远不命中、每次都花钱」，
而且不会报错，只会悄悄变贵。

这里集中做三件事：

1. **从库里装词表**：商品 / 别名 / 单位换算 / 往来单位（**只装 TopN 预算内的**，
   不把全量档案塞进 prompt，H12）；
2. **装配流水线与 provider**：provider 类型由 ``Settings`` 决定，
   fake/real 的「生产禁用」由 provider 自己保证（构造期就炸）；
3. **把解析结果翻译成落账明细**：``product_id`` / ``qty_base`` / 金额 / 付款状态，
   逐商品一行（``*_multi`` 出 N 行）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.intent_registry import IntentRegistry, default_registry, is_pure_entity_intent
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import PersonalDict
from app.models.partner import Partner, PartnerAlias
from app.services.cost_monitor import CostMonitor
from app.services.degrade import DegradeController
from app.voice.alias_resolver import AliasResolver, ProductEntry
from app.voice.asr_provider import AsrProvider, build_asr_provider
from app.voice.clarify_fsm import ClarifyFSM
from app.voice.gate import Gate
from app.voice.llm_fallback import LlmFallback
from app.voice.llm_provider import CountingLlmProvider, LlmProvider, build_llm_provider
from app.voice.non_command_filter import NonCommandFilter
from app.voice.output_schema import ParseResult
from app.voice.pipeline import VoicePipeline
from app.voice.prompt_builder import PromptBudget, PromptContext
from app.voice.prompt_cache import PromptCache
from app.voice.template_engine import TemplateEngine
from app.voice.unit_converter import standard_weight_ratio

__all__ = [
    "CATALOG_MATCH_LIMIT",
    "LedgerLine",
    "SharedVoiceState",
    "VoiceRuntime",
    "build_runtime",
    "invalidate_shared_prompt_cache",
    "lines_of_result",
    "load_partner_lexicon",
    "load_product_lexicon",
    "reset_shared_voice_state",
    "shared_voice_state",
]

#: 装进 G4 LLM prompt 的商品上限（架构 §2.4 H12：Top20，禁止全量）
PROMPT_TOP_PRODUCTS = 20

#: 本机规则/实体匹配词表上限（不进 prompt，只给规则与模糊消歧，避免 20 个以后的商品失明）
CATALOG_MATCH_LIMIT = 500

#: 往来单位上限（欠款查询/收款用不到全部）
PROMPT_TOP_PARTNERS = 50


@dataclass
class SharedVoiceState:
    """进程级共享语音观测与缓存状态（避免每请求重建空缓存与空成本监控）。"""

    prompt_cache: PromptCache
    cost_monitor: CostMonitor
    degrade: DegradeController


_VOICE_STATE_LOCK = threading.Lock()
_SHARED_VOICE_STATE: SharedVoiceState | None = None


def shared_voice_state(settings: Settings | None = None) -> SharedVoiceState:
    """取（或初始化）进程级共享 PromptCache / CostMonitor / DegradeController。"""
    global _SHARED_VOICE_STATE
    with _VOICE_STATE_LOCK:
        if _SHARED_VOICE_STATE is None:
            max_entries = int(getattr(settings, "prompt_cache_max_entries", 256) or 256)
            ttl = int(getattr(settings, "prompt_cache_ttl_seconds", 3600) or 3600)
            cache = PromptCache(max_entries=max_entries, ttl_seconds=ttl)
            _SHARED_VOICE_STATE = SharedVoiceState(
                prompt_cache=cache,
                cost_monitor=CostMonitor(prompt_cache=cache),
                degrade=DegradeController(),
            )
        return _SHARED_VOICE_STATE


def invalidate_shared_prompt_cache(tenant_id: str) -> int:
    """商品或个人词表变更时击穿共享 PromptCache。"""
    state = _SHARED_VOICE_STATE
    if state is None:
        return 0
    return state.prompt_cache.invalidate_tenant(tenant_id)


def reset_shared_voice_state() -> None:
    """清空共享状态（仅给测试用）。"""
    global _SHARED_VOICE_STATE
    with _VOICE_STATE_LOCK:
        _SHARED_VOICE_STATE = None


@dataclass
class VoiceRuntime:
    """一条装配好的语音运行时（词表按请求隔离，缓存与观测共享）。"""

    pipeline: VoicePipeline
    provider: CountingLlmProvider
    asr: AsrProvider
    products: AliasResolver
    partners: AliasResolver
    store_name: str
    contract_version: str
    registry: IntentRegistry = field(default_factory=default_registry)
    customers: AliasResolver = field(default_factory=lambda: AliasResolver(()))
    suppliers: AliasResolver = field(default_factory=lambda: AliasResolver(()))
    personal_fixes: Mapping[str, str] = field(default_factory=dict)
    dict_version: int = 0

    def llm_calls(self) -> int:
        """本次运行时累计模型调用次数（成本归因）。"""
        return int(self.provider.calls)


def load_product_lexicon(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
    limit: int = CATALOG_MATCH_LIMIT,
) -> list[ProductEntry]:
    """从库里装商品词表（正名 + 别名 + 已审核换算系数，含个人与租户别名隔离）。"""
    products = list(
        session.execute(
            select(Product)
            .where(Product.tenant_id == tenant_id, Product.status.in_(("draft", "active")))
            .order_by(Product.created_at.desc())
            .limit(max(1, limit))
        ).scalars()
    )
    if not products:
        return []
    ids = [product.product_id for product in products]

    alias_stmt = select(ProductAlias).where(
        ProductAlias.tenant_id == tenant_id,
        ProductAlias.product_id.in_(ids),
    )
    if member_id is not None:
        alias_stmt = alias_stmt.where(
            or_(ProductAlias.member_id.is_(None), ProductAlias.member_id == member_id)
        )
    else:
        alias_stmt = alias_stmt.where(ProductAlias.member_id.is_(None))

    aliases: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for row in session.execute(alias_stmt).scalars():
        aliases.setdefault(row.product_id, []).append((str(row.alias_text), str(row.source)))

    ratios: dict[uuid.UUID, dict[str, float]] = {}
    for row in session.execute(
        select(UnitConversion).where(
            UnitConversion.tenant_id == tenant_id,
            UnitConversion.product_id.in_(ids),
            UnitConversion.status == "approved",
        )
    ).scalars():
        ratios.setdefault(row.product_id, {})[str(row.from_unit)] = float(row.ratio)

    entries: list[ProductEntry] = []
    for product in products:
        rows = aliases.get(product.product_id, [])
        rows.sort(key=lambda item: 0 if item[1] == "personal" else 1)
        entries.append(
            ProductEntry(
                product_id=str(product.product_id),
                name=str(product.name),
                base_unit=str(product.base_unit),
                spec=str(product.spec or ""),
                aliases=tuple(text for text, _source in rows),
                last_purchase_price_fen=(
                    int(product.last_purchase_price_fen)
                    if product.last_purchase_price_fen is not None
                    else None
                ),
                last_sale_price_fen=(
                    int(product.last_sale_price_fen)
                    if product.last_sale_price_fen is not None
                    else None
                ),
                ratios=ratios.get(product.product_id, {}),
            )
        )
    return entries


def _load_member_personal_dict(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    products: Sequence[ProductEntry],
) -> tuple[dict[str, str], dict[str, str], int]:
    """从 ``personal_dict`` 读个人商品映射与个人口癖版本。"""
    if member_id is None:
        return {}, {}, 0
    rows = list(
        session.execute(
            select(PersonalDict).where(
                PersonalDict.tenant_id == tenant_id,
                PersonalDict.member_id == member_id,
            )
        ).scalars()
    )
    by_id = {entry.product_id: entry for entry in products}
    personal_aliases: dict[str, str] = {}
    personal_fixes: dict[str, str] = {}
    max_version = 0
    for row in rows:
        max_version = max(max_version, int(row.version or 0))
        if row.mapped_type == "product" and row.mapped_id is not None:
            pid = str(row.mapped_id)
            if pid in by_id:
                personal_aliases[str(row.raw_text)] = pid
    return personal_aliases, personal_fixes, max_version


def load_partner_lexicon(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int = PROMPT_TOP_PARTNERS,
    partner_type: str | None = None,
) -> list[ProductEntry]:
    """从库里装往来单位词表（正名 + 别名）。

    Args:
        session: 会话。
        tenant_id: 租户。
        limit: 最多装多少个（控 prompt 体积）。
        partner_type: ``customer`` / ``supplier``；``None`` 表示不过滤。
            方向判据只认「客户表 / 供应商表分别命中」，
            所以调用方要按类型各装一份（见 :func:`build_runtime`）。

    Returns:
        :class:`ProductEntry` 列表（``product_id`` 放的是 ``partner_id``）。
    """
    stmt = select(Partner).where(Partner.tenant_id == tenant_id, Partner.status == "active")
    if partner_type is not None:
        stmt = stmt.where(Partner.type == partner_type)
    partners = list(session.execute(stmt.limit(max(1, limit))).scalars())
    if not partners:
        return []
    ids = [partner.partner_id for partner in partners]
    aliases: dict[uuid.UUID, list[str]] = {}
    for row in session.execute(
        select(PartnerAlias).where(
            PartnerAlias.tenant_id == tenant_id, PartnerAlias.partner_id.in_(ids)
        )
    ).scalars():
        aliases.setdefault(row.partner_id, []).append(str(row.alias_text))
    return [
        ProductEntry(
            product_id=str(partner.partner_id),
            name=str(partner.name),
            aliases=tuple(aliases.get(partner.partner_id, [])),
        )
        for partner in partners
    ]


def build_runtime(
    session: Session,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
    store_name: str = "主仓",
    registry: IntentRegistry | None = None,
) -> VoiceRuntime:
    """装配语音运行时（词表来自 DB，provider 来自配置）。

    Args:
        session: 会话。
        settings: 配置。
        tenant_id: 租户。
        member_id: 员工（个人词表优先）。
        store_name: 门店名（缺省填充 ``W``）。
        registry: 意图注册表。

    Returns:
        :class:`VoiceRuntime`。
    """
    reg = registry or default_registry()
    product_entries = load_product_lexicon(
        session,
        tenant_id=tenant_id,
        member_id=member_id,
        limit=max(CATALOG_MATCH_LIMIT, int(settings.prompt_top_products)),
    )
    personal_aliases, personal_fixes, dict_version = _load_member_personal_dict(
        session,
        tenant_id=tenant_id,
        member_id=member_id,
        products=product_entries,
    )
    partner_entries = load_partner_lexicon(session, tenant_id=tenant_id)
    # 方向判据要「命中哪张表就是什么类型」，所以按 partner.type 各装一份
    customer_entries = load_partner_lexicon(
        session, tenant_id=tenant_id, partner_type="customer"
    )
    supplier_entries = load_partner_lexicon(
        session, tenant_id=tenant_id, partner_type="supplier"
    )

    provider: LlmProvider = build_llm_provider(settings)
    counter = provider if isinstance(provider, CountingLlmProvider) else CountingLlmProvider(provider)
    state = shared_voice_state(settings)
    budget = PromptBudget(
        top_products=int(settings.prompt_top_products),
        recent_contexts=int(settings.prompt_recent_contexts),
    )
    pipeline = VoicePipeline(
        engine=TemplateEngine(registry=reg, store_name=store_name),
        fallback=LlmFallback(
            counter,
            cache=state.prompt_cache,
            max_retries=settings.llm_schema_retry,
            registry=reg,
        ),
        non_command_filter=NonCommandFilter(),
        gate=Gate(),
        clarify=ClarifyFSM(registry=reg),
        registry=reg,
        provider=counter,
        budget=budget,
        contract_version=reg.version,
        store_name=store_name,
    )
    return VoiceRuntime(
        pipeline=pipeline,
        provider=counter,
        asr=build_asr_provider(settings),
        products=AliasResolver(product_entries, personal_aliases=personal_aliases),
        partners=AliasResolver(partner_entries),
        customers=AliasResolver(customer_entries),
        suppliers=AliasResolver(supplier_entries),
        store_name=store_name,
        contract_version=reg.version,
        registry=reg,
        personal_fixes=personal_fixes,
        dict_version=dict_version,
    )


@dataclass(frozen=True)
class LedgerLine:
    """落账明细的一行（卡片 ④ 区与落账共用）。"""

    product_id: str | None
    product_label: str
    quantity: Any
    unit: str
    qty_base: Decimal
    unit_price_fen: int | None
    amount_fen: int
    partner_id: str | None = None
    payment_status: str = "已结"
    calc_trace: str | None = None
    pending: bool = False
    similar_products: tuple[Mapping[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态（``confirm_card.slots.lines`` 里存的就是它）。"""
        data: dict[str, Any] = {
            "product_id": self.product_id,
            "product_label": self.product_label,
            "quantity": self.quantity,
            "unit": self.unit,
            "qty_base": str(self.qty_base),
            "unit_price_fen": self.unit_price_fen,
            "amount_fen": self.amount_fen,
            "partner_id": self.partner_id,
            "payment_status": self.payment_status,
            "calc_trace": self.calc_trace,
            "pending": self.pending,
        }
        if self.similar_products:
            data["similar_products"] = [dict(item) for item in self.similar_products]
        return data


def lines_of_result(
    result: ParseResult,
    *,
    runtime: VoiceRuntime,
    items: Sequence[Any] = (),
    partner_id: str | None = None,
) -> list[LedgerLine]:
    """把解析结果翻译成落账明细（多商品出 N 行）。

    Args:
        result: 解析结果。
        runtime: 运行时（用它的词表把商品名落到 ``product_id``）。
        items: 多商品片段（``TemplateOutcome.items``）。
        partner_id: 往来单位 id（有就带上，欠款用）。

    Returns:
        逐商品明细。
    """
    payment_status = str(result.slots.get("PS") or "已结")
    lines: list[LedgerLine] = []
    # 纯实体意图（只写档案、不写单据/库存/应收）：明细行不参与「数量口径是否可信」的
    # 判定。否则 product.create 的卡片一出生就带一条 pending 行，确认必失败（P0-2）。
    entity_only = is_pure_entity_intent(runtime.registry.get(result.intent))

    # **单商品意图也会带 1 个 item**（``_parse_items`` 造的是只有文本、没有数量/金额的壳），
    # 所以「items 非空」不等于「多商品」。只按 items 分支走会让「进了两箱可乐，一件三十五」
    # 落成数量为 0、金额为 0 的空单据（库存不动、账面上的金额是 0）——这是记错账，不是小问题。
    # 判定标准改成：这个 item **真的带了数量或金额**才走多商品分支。
    usable_items = [
        item
        for item in items
        if getattr(item, "quantity", None) is not None
        or int(getattr(item, "total_fen", 0) or 0) > 0
    ]

    if usable_items:
        for item in usable_items:
            name = getattr(item, "product_name", None)
            product = _resolve(runtime.products, name) if name else None
            quantity = getattr(item, "quantity", None)
            unit = str(getattr(item, "unit", "") or "")
            amount_fen = int(getattr(item, "total_fen", 0) or 0)
            unit_price_fen = getattr(item, "unit_price_fen", None)
            qty_base, ratio_resolved = _qty_base_of(product, quantity, unit)
            similars = (
                tuple(
                    {"product_id": e.product_id, "name": e.display_name()}
                    for e in runtime.products.find_similar(str(name or ""), limit=3)
                )
                if product is None and name
                else ()
            )
            lines.append(
                LedgerLine(
                    product_id=None if product is None else product.product_id,
                    product_label=_label_of(product, name),
                    quantity=quantity,
                    unit=unit or (product.base_unit if product else ""),
                    qty_base=qty_base,
                    unit_price_fen=None if unit_price_fen is None else int(unit_price_fen),
                    amount_fen=amount_fen,
                    partner_id=partner_id,
                    payment_status=payment_status,
                    calc_trace=(
                        None
                        if unit_price_fen is None
                        else f"{quantity}{unit}×{unit_price_fen}分={amount_fen}分"
                    ),
                    pending=False
                    if entity_only
                    else (product is None or quantity is None or not ratio_resolved),
                    similar_products=similars,
                )
            )
        if lines:
            return lines

    product_slot = result.slots.get("P")
    product = _resolve(runtime.products, product_slot) if product_slot else None
    quantity = result.slots.get("Q")
    unit = str(result.slots.get("U") or (product.base_unit if product else ""))
    amount_fen = int(result.slots.get("TA") or result.slots.get("AMT") or 0)
    if amount_fen == 0 and result.slots.get("UP") is not None and quantity is not None:
        amount_fen = int(
            (Decimal(str(result.slots["UP"])) * _as_decimal(quantity)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    up = result.slots.get("UP")
    qty_base, ratio_resolved = _qty_base_of(product, quantity, unit)
    similars = (
        tuple(
            {"product_id": e.product_id, "name": e.display_name()}
            for e in runtime.products.find_similar(str(product_slot or ""), limit=3)
        )
        if product is None and product_slot
        else ()
    )
    return [
        LedgerLine(
            product_id=None if product is None else product.product_id,
            product_label=_label_of(product, product_slot),
            quantity=quantity,
            unit=unit,
            qty_base=qty_base,
            unit_price_fen=None if up is None else int(up),
            amount_fen=amount_fen,
            partner_id=partner_id,
            payment_status=payment_status,
            calc_trace=result.calc_traces[0] if result.calc_traces else None,
            pending=False
            if entity_only
            else (
                product_slot is None
                or result.source.get("P") == "pending"
                or product is None
                or not ratio_resolved
            ),
            similar_products=similars,
        )
    ]


def _resolve(resolver: AliasResolver, name: Any) -> ProductEntry | None:
    """用词表把名字解析成商品/往来单位（支持精确、个人别名与高确信拼音模糊）。"""
    if not name:
        return None
    text_value = str(name).strip()
    if not text_value:
        return None
    try:
        hit = resolver.resolve(text_value)
    except Exception:  # noqa: BLE001 - 词表异常不该让整单失败
        hit = None
    if hit is not None and hit.product is not None:
        return hit.product
    lexicon = getattr(resolver, "lexicon", None)
    if lexicon is None:
        return None
    for candidate in (text_value, text_value.split(" ")[0]):
        try:
            entry = lexicon.lookup(candidate)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is not None and getattr(entry, "product", None) is not None:
            return entry.product
    fuzzy_fn = getattr(resolver, "fuzzy_match", None)
    if callable(fuzzy_fn):
        try:
            fuzzy_hit = fuzzy_fn(text_value, min_score=0.84, margin=0.08)
        except Exception:  # noqa: BLE001
            fuzzy_hit = None
        if fuzzy_hit is not None and getattr(fuzzy_hit, "product", None) is not None:
            return fuzzy_hit.product
    return None


def _label_of(product: ProductEntry | None, fallback: Any) -> str:
    """展示名：``可乐 500ml``（有规格就带上）。"""
    if product is None:
        return str(fallback or "待确认")
    return f"{product.name} {product.spec}".strip()


def _qty_base_of(
    product: ProductEntry | None, quantity: Any, unit: str
) -> tuple[Decimal, bool]:
    """折算到 base_unit：先查已审核档案系数，再查标准度量衡常数；缺包装系数不臆造。"""
    qty = _as_decimal(quantity)
    if product is None:
        return qty, False
    if not unit or unit == product.base_unit:
        return qty, True
    ratio = product.ratios.get(unit)
    if ratio is not None:
        return qty * Decimal(str(ratio)), True
    std = standard_weight_ratio(unit, product.base_unit)
    if std is not None:
        return qty * std, True
    return qty, False


def _as_decimal(value: Any) -> Decimal:
    """安全转 Decimal。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def prompt_context_of(
    *, user_text: str, today_iso: str, tenant_id: str, contract_version: str, domain_hint: str | None
) -> PromptContext:
    """构造 LLM 兜底用的 prompt 上下文（路由层复用，避免各处拼错）。"""
    return PromptContext(
        user_text=user_text,
        today_iso=today_iso,
        tenant_id=tenant_id,
        contract_version=contract_version,
        domain_hint=domain_hint,
    )


def iter_product_names(entries: Iterable[ProductEntry]) -> tuple[str, ...]:
    """词表里的正名（测试与调试用）。"""
    return tuple(entry.name for entry in entries)


def product_id_of(entries: Sequence[ProductEntry], name: str) -> str | None:
    """按正名找 id（测试夹具用）。"""
    for entry in entries:
        if entry.name == name:
            return entry.product_id
    return None


def placeholder_ids() -> Mapping[str, str]:
    """空词表场景的占位（显式空映射，避免各处写 ``{}`` 产生歧义）。"""
    return {}
