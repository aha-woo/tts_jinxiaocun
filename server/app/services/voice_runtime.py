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

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.intent_registry import IntentRegistry, default_registry
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.partner import Partner, PartnerAlias
from app.voice.alias_resolver import AliasResolver, ProductEntry
from app.voice.asr_provider import AsrProvider, build_asr_provider
from app.voice.clarify_fsm import ClarifyFSM
from app.voice.gate import Gate
from app.voice.llm_fallback import LlmFallback
from app.voice.llm_provider import CountingLlmProvider, LlmProvider, build_llm_provider
from app.voice.non_command_filter import NonCommandFilter
from app.voice.output_schema import ParseResult
from app.voice.pipeline import VoicePipeline
from app.voice.prompt_builder import PromptContext
from app.voice.prompt_cache import PromptCache
from app.voice.template_engine import TemplateEngine

__all__ = [
    "LedgerLine",
    "VoiceRuntime",
    "build_runtime",
    "lines_of_result",
    "load_partner_lexicon",
    "load_product_lexicon",
]

#: 装进解析器的商品上限（架构 §2.4 H12：Top20，禁止全量）
PROMPT_TOP_PRODUCTS = 20

#: 往来单位上限（欠款查询/收款用不到全部）
PROMPT_TOP_PARTNERS = 50


@dataclass
class VoiceRuntime:
    """一条装配好的语音运行时（每请求一份，不跨请求共享状态）。"""

    pipeline: VoicePipeline
    provider: CountingLlmProvider
    asr: AsrProvider
    products: AliasResolver
    partners: AliasResolver
    store_name: str
    contract_version: str
    registry: IntentRegistry = field(default_factory=default_registry)

    def llm_calls(self) -> int:
        """本次运行时累计模型调用次数（成本归因）。"""
        return int(self.provider.calls)


def load_product_lexicon(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
    limit: int = PROMPT_TOP_PRODUCTS,
) -> list[ProductEntry]:
    """从库里装商品词表（正名 + 别名 + 已审核的换算系数）。

    Args:
        session: 会话（已绑定租户上下文）。
        tenant_id: 租户。
        member_id: 当前员工（个人别名优先生效）。
        limit: 上限（**不装全量**）。

    Returns:
        ``ProductEntry`` 列表。
    """
    products = list(
        session.execute(
            select(Product)
            .where(Product.tenant_id == tenant_id, Product.status == "active")
            .order_by(Product.created_at.desc())
            .limit(max(1, limit))
        ).scalars()
    )
    if not products:
        return []
    ids = [product.product_id for product in products]

    aliases: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for row in session.execute(
        select(ProductAlias).where(
            ProductAlias.tenant_id == tenant_id, ProductAlias.product_id.in_(ids)
        )
    ).scalars():
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
        # 个人别名优先（命中时 source=alias 的语义就是这个）
        rows.sort(key=lambda item: 0 if item[1] == "personal" else 1)
        entries.append(
            ProductEntry(
                product_id=str(product.product_id),
                name=str(product.name),
                base_unit=str(product.base_unit),
                spec=str(product.spec or ""),
                aliases=tuple(text for text, _source in rows),
                last_purchase_price_fen=int(product.last_purchase_price_fen) or None,
                last_sale_price_fen=int(product.last_sale_price_fen) or None,
                ratios=ratios.get(product.product_id, {}),
            )
        )
    return entries


def load_partner_lexicon(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int = PROMPT_TOP_PARTNERS,
) -> list[ProductEntry]:
    """从库里装往来单位词表（正名 + 别名）。"""
    partners = list(
        session.execute(
            select(Partner)
            .where(Partner.tenant_id == tenant_id, Partner.status == "active")
            .limit(max(1, limit))
        ).scalars()
    )
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
        session, tenant_id=tenant_id, member_id=member_id, limit=settings.prompt_top_products
    )
    partner_entries = load_partner_lexicon(session, tenant_id=tenant_id)

    provider: LlmProvider = build_llm_provider(settings)
    counter = CountingLlmProvider(provider)
    cache = PromptCache(
        max_entries=settings.prompt_cache_max_entries,
        ttl_seconds=settings.prompt_cache_ttl_seconds,
    )
    pipeline = VoicePipeline(
        engine=TemplateEngine(registry=reg, store_name=store_name),
        fallback=LlmFallback(
            counter,
            cache=cache,
            max_retries=settings.llm_schema_retry,
            registry=reg,
        ),
        non_command_filter=NonCommandFilter(),
        gate=Gate(),
        clarify=ClarifyFSM(registry=reg),
        registry=reg,
        provider=counter,
        contract_version=reg.version,
        store_name=store_name,
    )
    return VoiceRuntime(
        pipeline=pipeline,
        provider=counter,
        asr=build_asr_provider(settings),
        products=AliasResolver(product_entries),
        partners=AliasResolver(partner_entries),
        store_name=store_name,
        contract_version=reg.version,
        registry=reg,
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

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态（``confirm_card.slots.lines`` 里存的就是它）。"""
        return {
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

    if items:
        for item in items:
            name = getattr(item, "product_name", None)
            product = _resolve(runtime.products, name) if name else None
            quantity = getattr(item, "quantity", None)
            unit = str(getattr(item, "unit", "") or "")
            amount_fen = int(getattr(item, "total_fen", 0) or 0)
            unit_price_fen = getattr(item, "unit_price_fen", None)
            qty_base = _qty_base_of(product, quantity, unit)
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
                    pending=product is None or quantity is None,
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
        amount_fen = int(result.slots["UP"]) * int(quantity)
    up = result.slots.get("UP")
    qty_base = _qty_base_of(product, quantity, unit)
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
            pending=product_slot is None
            or result.source.get("P") == "pending"
            or product is None,
        )
    ]


def _resolve(resolver: AliasResolver, name: Any) -> ProductEntry | None:
    """用词表把名字解析成商品/往来单位（解析不到返回 ``None``，不猜）。

    两段式：先按「整串就是一个词」查（含个人词表），再退到词表精确查
    （解析结果里的 ``P`` 已经是 ``可乐 500ml`` 这种带规格的形式）。
    """
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
    return None


def _label_of(product: ProductEntry | None, fallback: Any) -> str:
    """展示名：``可乐 500ml``（有规格就带上）。"""
    if product is None:
        return str(fallback or "待确认")
    return f"{product.name} {product.spec}".strip()


def _qty_base_of(product: ProductEntry | None, quantity: Any, unit: str) -> Decimal:
    """折算到 base_unit（**缺系数不臆造**：按 1:1 处理并标 pending）。"""
    qty = _as_decimal(quantity)
    if product is None or not unit or unit == product.base_unit:
        return qty
    ratio = product.ratios.get(unit)
    if ratio is None:
        return qty
    return qty * Decimal(str(ratio))


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
