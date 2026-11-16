"""G3 规则引擎：**原话 → ParseResult**，全程 0 次模型调用（最大省钱点）。

流程（对应架构 §4 ③）
----------------------
1. **切片段**：数量+单位、金额（单价/总价）、付款状态、日期、换算系数、
   目标字段、别名、单据引用、备注；
2. **认商品**：``alias_resolver`` 最长匹配；命中多个 → 不猜，给候选；
3. **认意图**：``template_rule.match_rules`` 按优先级链取最优；
4. **填槽位**：``slot_filler`` 套契约缺省规则 + 金额自洽；
5. **冲突复核**：``conflict_detector`` 反向质疑（快照当增量这类错**必须**被拦下）；
6. **出结论**：``confidence`` 合成置信度 → 路由 ``card / answer / pending_task /
   static / clarify``。

未命中一律 ``escalate=True``，由 ``llm_fallback`` 接手（唯一计费点）。
本模块**不写库、不发网络请求、不建卡片**——纯函数式，可离线全量回归。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.utils.unit import CANONICAL_UNITS
from app.domain.intent_registry import IntentRegistry, IntentSpec, default_registry
from app.voice.alias_resolver import AliasHit, AliasResolver, ProductEntry
from app.voice.amount_calculator import AmountOutcome, compute_amounts
from app.voice.clarifier_templates import (
    ClarifierCatalog,
    build_clarify,
    default_catalog,
    pick_slot_to_ask,
)
from app.voice.confidence import (
    BAND_MID,
    ConfidenceBreakdown,
    combine,
    should_card,
)
from app.voice.conflict_detector import (
    Conflict,
    ConflictContext,
    blocking_conflicts,
    detect,
)
from app.voice.direction import (
    DirectionVerdict,
    build_direction_clarify,
    is_trade_intent,
    resolve_direction,
    switch_trade_intent,
)
from app.voice.output_schema import ClarifyPrompt, ParseResult, validate_parse_result
from app.voice.slot_filler import SlotFillResult, SlotFiller
from app.voice.template_rule import (
    RULE_TABLE,
    RuleMatch,
    SlotSpan,
    best_match,
    extract_alias_span,
    extract_date_span,
    extract_doc_ref_span,
    extract_note_span,
    extract_payment_span,
    extract_quantity_spans,
    extract_ratio_span,
    extract_target_field_span,
    is_interrogative,
    match_rules,
    split_items,
)

__all__ = [
    "ParsedItem",
    "PartyHits",
    "TemplateEngine",
    "TemplateOutcome",
    "extract_product_name_fallback",
    "scan_items",
]

#: 商品名兜底抽取时要剔除的噪音词（动词 / 结构词 / 货币词）
_PRODUCT_NOISE: tuple[str, ...] = (
    "今天",
    "今儿",
    "昨日",
    "昨天",
    "刚才",
    "刚",
    "已经",
    "先",
    "那个",
    "这个",
    "还有",
    "一共",
    "总共",
    "合计",
    "都",
    "按",
    "给",
    "说",
    "记",
    "上",
    "把",
    "行",
    "就",
    "点",
    "个货",
    "货架",
    "货架上",
    "仓库",
    "架子",
    "货",
    "拉",
    "拿",
    "走",
)

#: 商品名兜底抽取时要整段丢弃的子句线索（金额/付款/时间类）
_DROP_CLAUSE_CUES: tuple[str, ...] = (
    "一共",
    "总共",
    "合计",
    "块",
    "元",
    "钱",
    "付",
    "欠",
    "微信",
    "现金",
    "转账",
    "记账",
    "老价",
    "每",
    "等于",
    "下回",
    "这回",
    "以后",
    "再说",
    "已经",
)

#: 写动词（兜底抽取商品名前先剥掉）
_VERB_WORDS: tuple[str, ...] = (
    "进点货",
    "进了",
    "进货",
    "上货",
    "采购",
    "拿货",
    "提货",
    "到了",
    "拉到",
    "拉了",
    "拿了",
    "卖了",
    "卖",
    "出库",
    "拿走",
    "要了",
    "提走",
    "发出了",
    "盘点",
    "盘货",
    "盘了",
    "盘下",
    "盘一下",
    "数了下",
    "数一下",
    "点数",
    "清点",
    "还剩",
    "剩下",
    "还有",
    "再入",
    "又进",
    "补了",
    "补货",
    "报损",
    "损耗",
    "坏了",
    "少了",
    "对不上",
    "短少",
    "破了",
    "烂了",
    "过期了",
    "摔了",
    "加个新品",
    "新增商品",
    "新品",
    "建档",
    "加个商品",
    "新建商品",
    "别名",
    "以后",
)


@dataclass(frozen=True)
class PartyHits:
    """句中出现过的往来对象（**按类型分开**，方向判据的 ``partner.type`` 来源）。"""

    customer_names: tuple[str, ...] = ()
    supplier_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedItem:
    """多商品句里的一个商品片段（``*_multi`` 意图用；T04 负责展开成多行）。"""

    text: str
    product_name: str | None = None
    quantity: Any = None
    unit: str | None = None
    unit_price_fen: int | None = None
    total_fen: int | None = None
    product_ambiguous: bool = False
    product_options: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """商品与数量都认出来了才算可用。"""
        return self.product_name is not None and self.quantity is not None


@dataclass
class TemplateOutcome:
    """G3 结论（``hit=False`` 时调用方必须升级到 G4）。"""

    hit: bool
    reason: str
    result: ParseResult | None = None
    match: RuleMatch | None = None
    spec: IntentSpec | None = None
    slots: SlotFillResult | None = None
    spans: dict[str, SlotSpan] = field(default_factory=dict)
    items: tuple[ParsedItem, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    confidence: ConfidenceBreakdown | None = None
    clarify: ClarifyPrompt | None = None
    unknown_spans: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()
    escalate: bool = False
    domain_hint: str | None = None

    @property
    def intent_id(self) -> str | None:
        """命中的意图（未命中为 ``None``）。"""
        return self.result.intent if self.result is not None else None

    @property
    def route(self) -> str | None:
        """路由结论。"""
        return self.result.route if self.result is not None else None

    @property
    def blocked(self) -> bool:
        """是否被阻断级冲突拦下（不得出写卡）。"""
        return bool(blocking_conflicts(self.conflicts))


_CN_NUMERAL_CHARS = frozenset("零〇一二两三四五六七八九十百千万亿幺拐勾俩仨壹贰叁肆伍陆柒捌玖拾佰仟点")

_STOPWORD_NAMES: frozenset[str] = frozenset(
    {
        "货",
        "东西",
        "商品",
        "这个",
        "那个",
        "什么",
        "多少",
        "钱",
        "单价",
        "总价",
        # 位置/账簿类词：常出现在句尾，绝不是商品名
        "货架",
        "仓库",
        "店里",
        "门店",
        "架子",
        "账",
        "账上",
        "本上",
        "单子",
    }
)


def _is_pure_number(text: str) -> bool:
    """是否整串都是「数字 + 单位」（如「三十五」「一件三十五」「3 箱」）。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    for unit in CANONICAL_UNITS:
        cleaned = cleaned.replace(unit, "")
    return all(char.isdigit() or char in _CN_NUMERAL_CHARS for char in cleaned)


def _is_cjk(char: str) -> bool:
    """是否汉字。"""
    return "\u4e00" <= char <= "\u9fff"


#: 一次剔除所有「数字+单位」片段（兜底抽名用）
_QUANTITY_STRIPPER = re.compile(
    rf"\d+(?:\.\d+)?\s*(?:{'|'.join(CANONICAL_UNITS)})|"
    rf"[零〇一二两三四五六七八九十百千万幺拐勾俩仨]+\s*(?:{'|'.join(CANONICAL_UNITS)})"
)

#: 噪音词按长度降序（长词优先替换，避免短词先把长词切碎）
_NOISE_SORTED: tuple[str, ...] = tuple(
    sorted((word for word in _PRODUCT_NOISE if word), key=len, reverse=True)
)


def extract_product_name_fallback(text: str, *, exclude: Iterable[str] = ()) -> str | None:
    """在档案没命中时，从原话里**保守**剥出一个商品名候选。

    只做「剔除已知噪音后剩下的名词串」，不猜规格、不猜品牌。
    剥不出干净的串就返回 ``None``（调用方追问，不许硬填）。
    """
    if not text:
        return None
    remaining = text
    for word in _VERB_WORDS:
        remaining = remaining.replace(word, " ")
    # 剔除**全部**「数字+单位」（只剔第一处会让「一件三十五」漏成商品名）
    remaining = _QUANTITY_STRIPPER.sub(" ", remaining)
    # 长词优先：「货架」必须先于「货」被剔除，否则会剩下一个「架」冒充商品名
    for word in _NOISE_SORTED:
        remaining = remaining.replace(word, " ")
    remaining = remaining.replace(" ", " ")
    for name in exclude:
        if name:
            remaining = remaining.replace(name, " ")

    best: str | None = None
    for clause in split_items(remaining):
        if any(cue in clause for cue in _DROP_CLAUSE_CUES):
            continue
        cleaned = "".join(
            char
            for char in clause
            if char not in " \t，,。.、！!？?：:；;“”\"'（）()【】[]…—~·的是了个些这那"
        )
        if not cleaned or _is_pure_number(cleaned):
            continue
        if cleaned in _STOPWORD_NAMES:
            continue
        if len(cleaned) == 1 and not _is_cjk(cleaned):
            continue
        if best is None or len(cleaned) > len(best):
            best = cleaned
    return best


class TemplateEngine:
    """规则层引擎（G3）。"""

    def __init__(
        self,
        *,
        registry: IntentRegistry | None = None,
        rules: Sequence[Any] = RULE_TABLE,
        store_name: str = "主仓",
        catalog: ClarifierCatalog | None = None,
        store_names: Sequence[str] = (),
    ) -> None:
        self._registry = registry or default_registry()
        self._rules = tuple(rules)
        self._filler = SlotFiller(store_name=store_name)
        self._catalog = catalog or default_catalog()
        self._store_names = tuple(store_names)

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        text: str,
        *,
        today_iso: str,
        resolver: AliasResolver | None = None,
        customers: AliasResolver | None = None,
        suppliers: AliasResolver | None = None,
        last_date_iso: str | None = None,
        asr_confidence: float = 0.95,
        corrections: int = 0,
    ) -> TemplateOutcome:
        """执行一次规则层解析。

        Args:
            text: ASR 归一后的原话（已做同音纠正）。
            today_iso: 今天（``YYYY-MM-DD``），租户时区。
            resolver: 商品词表。
            customers: 客户词表。
            suppliers: 供应商词表。
            last_date_iso: 最近一单日期（「上次」锚点）。
            asr_confidence: ASR 置信度。
            corrections: 同音纠正处数（参与置信度扣分）。

        Returns:
            :class:`TemplateOutcome`
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return TemplateOutcome(hit=False, reason="empty_text", escalate=True)

        trace: list[str] = []
        # 1) 切片段
        qty_spans = extract_quantity_spans(cleaned, allow_zero=True)
        spans: dict[str, SlotSpan] = {}
        ambiguous: list[SlotSpan] = []
        for span in qty_spans:
            if span.ambiguous:
                ambiguous.append(span)
            if span.ok:
                spans[span.code] = span

        qty_value = spans["Q"].value if "Q" in spans else None
        unit_value = str(spans["U"].value) if "U" in spans and spans["U"].ok else ""

        amount: AmountOutcome = compute_amounts(cleaned, quantity=_as_decimal(qty_value), unit_label=unit_value)
        money_spans = _amount_spans(amount)
        for span in money_spans:
            spans.setdefault(span.code, span)
        if amount.traces:
            trace.extend(amount.traces)

        for extractor, kind in (
            (extract_payment_span, "payment"),
            (extract_ratio_span, "ratio"),
            (extract_target_field_span, "target_field"),
            (extract_alias_span, "alias"),
            (extract_doc_ref_span, "doc_ref"),
            (extract_note_span, "note"),
        ):
            span = extractor(cleaned)
            if span is not None:
                spans.setdefault(span.code, span)
                del kind  # 仅用于可读性，实际不参与逻辑

        date_span = extract_date_span(cleaned, today_iso=today_iso, last_date_iso=last_date_iso)
        if date_span is not None:
            spans.setdefault("D", date_span)

        # 2) 认商品 / 客户 / 供应商
        product_hits = resolver.scan(cleaned) if resolver is not None else ()
        product, product_ambiguous, product_options = _pick_product(product_hits)
        if product is not None:
            spans.setdefault(
                "P",
                SlotSpan(
                    code="P",
                    text=product.name,
                    start=0,
                    end=len(product.name),
                    value=product.display_name(),
                    source="alias" if any(hit.is_alias for hit in product_hits) else "asr",
                    confidence=0.95,
                ),
            )
        partner_name = _party_name(cleaned, customers=customers, suppliers=suppliers, exclude=product.name if product else "")
        # 往来对象**按类型**各记一份：命中哪张词表 = partner.type，方向判据靠它（0 成本）
        party = _party_hits(
            cleaned,
            customers=customers,
            suppliers=suppliers,
            exclude=product.name if product else "",
        )

        # 3) 认意图（「一句话多商品」由商品命中数判定，不靠标点猜）
        scanned_items = scan_items(cleaned, resolver=resolver)
        # 「一句话多商品」只认**在商品档案里真的命中**的片段：
        # 兜底猜出来的名字（``extract_product_name_fallback``）不算证据，
        # 否则「跟老张拿货，红牛一箱」这种只有一个真商品的话会被判成多商品，
        # 直接走成 ``*_create_multi``，是「记错账」级误判。
        multi_present = _resolved_item_count(cleaned, resolver=resolver) >= 2
        matches = match_rules(
            cleaned,
            rules=self._rules,
            quantity_present=bool(qty_spans),
            money_present=amount.matched or bool(amount.money_text),
            multi_present=multi_present,
        )
        best = best_match(matches)
        if best is None:
            return TemplateOutcome(
                hit=False,
                reason="no_rule_matched",
                spans=spans,
                conflicts=(),
                unknown_spans=(cleaned,),
                trace=tuple(trace),
                escalate=True,
                domain_hint=_domain_hint(cleaned),
            )

        spec = self._registry.require(best.intent_id)
        trace.append(best.explain())

        # 3.5) 方向判定（方案 d）：关键词只负责「认出这是一笔交易」，
        #      方向由「明确方向词 > partner.type > 收付信号」单独判；
        #      三类都没有 → **不猜**，强制澄清问一句（下一节 _decide_clarify）。
        direction: DirectionVerdict | None = None
        if is_trade_intent(best.intent_id):
            direction = resolve_direction(
                cleaned,
                has_customer=bool(party.customer_names),
                has_supplier=bool(party.supplier_names),
                customer_names=party.customer_names,
                supplier_names=party.supplier_names,
            )
            trace.append(direction.explain())
            switched = switch_trade_intent(best.intent_id, direction.direction)
            if switched is not None and switched != best.intent_id:
                trace.append(f"方向判定改写意图：{best.intent_id} → {switched}")
                spec = self._registry.require(switched)

        # 4) product.create 等「档案类」允许兜底抽名
        if "P" not in spans and "P" in spec.all_slots:
            candidate = extract_product_name_fallback(
                cleaned, exclude=tuple(name for name in (partner_name or "",) if name)
            )
            if candidate:
                spans["P"] = SlotSpan(
                    code="P",
                    text=candidate,
                    start=cleaned.find(candidate),
                    end=cleaned.find(candidate) + len(candidate),
                    value=candidate,
                    source="asr",
                    confidence=0.7,
                    note="未在商品档案中命中，按新增候选处理",
                )
                trace.append(f"P：原话候选「{candidate}」")

        # 5) 填槽位
        filtered = {code: span for code, span in spans.items() if code in spec.all_slots}
        filled = self._filler.fill(
            spec,
            filtered,
            today_iso=today_iso,
            product=product,
            partner_name=partner_name,
            last_date_iso=last_date_iso,
        )
        trace.extend(filled.traces)
        trace.extend(
            f"缺省 {code}={filled.slots.get(code)}（{note}）"
            for code, note in filled.defaults.items()
        )

        # 6) 冲突复核
        items = (
            scanned_items
            if spec.intent_id.endswith("_multi")
            else self._parse_items(cleaned, spec=spec, product=product)
        )
        conflicts = detect(
            ConflictContext(
                text=cleaned,
                intent_id=spec.intent_id,
                slots=filled.slots,
                source=filled.source,
                ambiguous_spans=tuple(ambiguous),
                amount_rule=amount.rule,
                product_ambiguous=product_ambiguous,
                product_options=product_options,
                date_needs_confirm=bool(date_span is not None and date_span.confidence < 0.7),
                corrections=corrections,
                store_names=self._store_names,
                has_ratio=not (
                    "U" in filled.slots and filled.source.get("U") == "pending"
                ),
            )
        )
        blocking = blocking_conflicts(conflicts)
        soft = tuple(conflict for conflict in conflicts if not conflict.blocked)

        # 7) 置信度 + 追问
        unknown = tuple(_unknown_spans(cleaned, spans, items))
        # ``pending`` 分两类，扣分口径必须不同：
        # - **必填槽位** pending = 关键信息缺失 → 该重扣并追问；
        # - **可选槽位** pending = 我们**诚实地留白**（例如没说供应商，S=「待补」），
        #   卡片上会带「待补」角标让用户自己补。把它当成「猜错了」按必填扣分，
        #   会让「进了两箱可乐，一件三十五」这类最常见、最该出卡的话**永远出不了卡**
        #   —— 这才是命中率上不去的真实原因，不是方向词不够。
        optional_pending = tuple(
            code for code in filled.pending if code not in spec.required_slots
        )
        required_pending = tuple(
            code for code in filled.pending if code in spec.required_slots
        )
        if optional_pending:
            trace.append(f"可选槽位留白（不扣置信度）：{list(optional_pending)}")
        confidence = combine(
            rule_score=best.score,
            asr_confidence=asr_confidence,
            defaults=tuple(filled.defaults),
            pending=required_pending,
            ambiguities=tuple(filled.ambiguities),
            blocking_conflicts=len(blocking),
            soft_conflicts=len(soft),
            unknown_spans=unknown,
        )

        clarify = self._decide_clarify(
            spec=spec,
            filled=filled,
            blocking=blocking,
            product_options=product_options,
            resolver=resolver,
            confidence_value=confidence.value,
            direction=direction,
        )

        route = _route_for(spec, clarify=clarify, confidence_value=confidence.value)
        if clarify is not None:
            route = "clarify"

        payload: dict[str, Any] = {
            "intent": spec.intent_id,
            "slots": dict(filled.slots),
            "source": dict(filled.source),
            "confidence": confidence.value,
            "clarify": clarify.model_dump() if clarify is not None else None,
            "calc_traces": list(filled.traces),
            "channel": "template",
            "unknown_spans": list(unknown),
            "route": route,
        }
        parsed = validate_parse_result(payload, registry=self._registry)

        return TemplateOutcome(
            hit=True,
            reason=best.rule.note or best.rule.rule_id,
            result=parsed,
            match=best,
            spec=spec,
            slots=filled,
            spans=spans,
            items=items,
            conflicts=conflicts,
            confidence=confidence,
            clarify=clarify,
            unknown_spans=unknown,
            trace=tuple(trace),
            escalate=False,
            domain_hint=spec.domain,
        )

    # ------------------------------------------------------------------ 内部
    def _decide_clarify(
        self,
        *,
        spec: IntentSpec,
        filled: SlotFillResult,
        blocking: tuple[Conflict, ...],
        product_options: tuple[str, ...],
        resolver: AliasResolver | None,
        confidence_value: float,
        direction: DirectionVerdict | None = None,
    ) -> ClarifyPrompt | None:
        """决定这一轮问什么（一次只问 1 个槽位）。

        **方向优先**：方向判错＝账记错，代价远高于「多问一句」，
        所以只要方向判不出来（或判据互相矛盾），第一句就问方向，别的槽位往后排。
        """
        if direction is not None and direction.direction is None:
            return build_direction_clarify(conflict=direction.conflict)
        for conflict in blocking:
            if conflict.clarify_slot:
                dynamic = conflict.options or None
                return build_clarify(
                    spec.intent_id,
                    conflict.clarify_slot,
                    catalog=self._catalog,
                    dynamic_options=list(dynamic) if dynamic else None,
                )
        if filled.missing_required:
            slot = pick_slot_to_ask(filled.missing_required, spec=spec)
            if slot is None:
                return None
            dynamic = product_options if slot == "P" and product_options else None
            return build_clarify(
                spec.intent_id,
                slot,
                catalog=self._catalog,
                dynamic_options=list(dynamic) if dynamic else None,
            )
        if filled.ambiguities and confidence_value < BAND_MID:
            first = filled.ambiguities[0]
            slot = first.split("：", 1)[0]
            return build_clarify(spec.intent_id, slot, catalog=self._catalog)
        if confidence_value < BAND_MID:
            # 低置信不出写卡：问最影响入账的一项。
            # 优先问**还没被原话确认**的必填槽位（pending / default / computed）；
            # 都比原话确认过就退回按契约顺序问第一个——问一个已经有答案的槽位
            # 用户也答不出新信息，但至少不会漏问关键项。
            doubtful = tuple(
                code
                for code in spec.required_slots
                if filled.source.get(code) in {"pending", "default", "computed"}
            )
            slot = (
                pick_slot_to_ask(doubtful, spec=spec)
                if doubtful
                else pick_slot_to_ask(tuple(spec.required_slots), spec=spec)
            ) or "Q"
            del resolver
            return build_clarify(spec.intent_id, slot, catalog=self._catalog)
        return None

    def _parse_items(
        self,
        text: str,
        *,
        spec: IntentSpec,
        product: ProductEntry | None,
    ) -> tuple[ParsedItem, ...]:
        """单商品意图的 item 视图（多商品见 :func:`scan_items`）。"""
        if product is None and "P" not in spec.all_slots:
            return ()
        return (
            ParsedItem(
                text=text,
                product_name=product.display_name() if product else None,
            ),
        )


# --------------------------------------------------------------------------- 工具
def _as_decimal(value: Any) -> Decimal | None:
    """把数量转成 ``Decimal``（金额反算需要精确十进制）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return None


def _amount_spans(outcome: AmountOutcome) -> tuple[SlotSpan, ...]:
    """把 ``AmountOutcome`` 转成 ``UP`` / ``TA`` 片段（``computed`` 必带计算式）。"""
    spans: list[SlotSpan] = []
    text = outcome.money_text
    if outcome.up_fen is not None:
        source = "computed" if "reverse" in outcome.rule else "asr"
        spans.append(
            SlotSpan(
                code="UP",
                text=text,
                start=0,
                end=len(text),
                value=int(outcome.up_fen),
                source=source,
                confidence=outcome.confidence,
                note=outcome.rule,
            )
        )
    if outcome.ta_fen is not None:
        source = "asr" if outcome.rule.startswith("total_hint") else "asr"
        spans.append(
            SlotSpan(
                code="TA",
                text=text,
                start=0,
                end=len(text),
                value=int(outcome.ta_fen),
                source=source,
                confidence=outcome.confidence,
                note=outcome.rule,
            )
        )
    return tuple(spans)


def _pick_product(
    hits: Sequence[AliasHit],
) -> tuple[ProductEntry | None, bool, tuple[str, ...]]:
    """从命中里挑唯一商品；有歧义时给候选（不猜）。"""
    ambiguous_hits = [hit for hit in hits if hit.ambiguous]
    if ambiguous_hits:
        longest = max(ambiguous_hits, key=lambda item: item.end - item.start)
        options: dict[str, None] = {}
        for entry in longest.candidates:
            options.setdefault(entry.display_name(), None)
        return None, True, tuple(options)
    resolved = [hit for hit in hits if hit.resolved and hit.product is not None]
    if not resolved:
        return None, False, ()
    longest = max(resolved, key=lambda item: item.end - item.start)
    return longest.product, False, ()


def _resolved_item_count(text: str, *, resolver: AliasResolver | None) -> int:
    """数「真的在商品档案里命中」的片段（多商品判定的唯一证据来源）。

    只有 ``_pick_product`` 拿到 ``ProductEntry``（或报歧义）才算数；
    兜底猜出来的名字只是候选，**不能**用来证明「这句话有两个商品」。
    """
    if resolver is None:
        return 0
    # 同一句话里**不同**的商品个数：按 ``product_id`` 去重，
    # 因此「可乐两箱红牛一箱」这种**没有顿号**的口语也能数出 2 个。
    product_ids: set[str] = set()
    guessed = 0
    for segment in split_items(text):
        hits = resolver.scan(segment)
        found = {
            hit.product.product_id for hit in hits if hit.resolved and hit.product is not None
        }
        if found:
            product_ids |= found
            continue
        _entry, ambiguous, _options = _pick_product(hits)
        if ambiguous:
            guessed += 1
            continue
        # 档案里没命中，但「有数量 + 有名字」的片段同样是独立的商品行
        # （多商品句的典型形态就是「红牛一件、和其正两件」，后者在演示词表里可能没建档）。
        # 只有**数量**才是证据：光一个猜出来的名字（「跟老张拿货」）不算第二个商品。
        has_qty = any(
            span.code == "Q" and span.ok
            for span in extract_quantity_spans(segment, allow_zero=True)
        )
        if has_qty and extract_product_name_fallback(segment):
            guessed += 1
    return len(product_ids) + guessed


def scan_items(text: str, *, resolver: AliasResolver | None = None) -> tuple[ParsedItem, ...]:
    """把一句话切成多商品候选（``*_multi`` 意图与多商品判定共用）。

    只按标点切分 + 逐段认商品，**不做跨段推断**；认不出商品的段直接跳过。
    """
    items: list[ParsedItem] = []
    for segment in split_items(text):
        hits = resolver.scan(segment) if resolver is not None else ()
        entry, ambiguous, options = _pick_product(hits)
        qty_spans = extract_quantity_spans(segment, allow_zero=True)
        qty = next((span.value for span in qty_spans if span.code == "Q" and span.ok), None)
        unit = next((str(span.value) for span in qty_spans if span.code == "U" and span.ok), None)
        amount = compute_amounts(segment, quantity=_as_decimal(qty), unit_label=unit or "")
        name = entry.display_name() if entry is not None else extract_product_name_fallback(segment)
        if name is None and qty is None:
            continue
        items.append(
            ParsedItem(
                text=segment,
                product_name=name,
                quantity=qty,
                unit=unit,
                unit_price_fen=amount.up_fen,
                total_fen=amount.ta_fen,
                product_ambiguous=ambiguous,
                product_options=options,
            )
        )
    return tuple(items)


def _party_hits(
    text: str,
    *,
    customers: AliasResolver | None,
    suppliers: AliasResolver | None,
    exclude: str = "",
) -> "PartyHits":
    """按**类型**收集句中出现的往来对象（方向判据用）。

    上游把客户与供应商分成两个词表，所以「命中哪张表」本身就等于 ``partner.type``：
    ``customers`` 命中 → 客户；``suppliers`` 命中 → 供应商；两边都命中 →
    同一家既是客户又是供应商，方向判据**不采纳**（见 :func:`resolve_direction`）。
    """
    return PartyHits(
        customer_names=_names_of(text, customers, exclude=exclude),
        supplier_names=_names_of(text, suppliers, exclude=exclude),
    )


def _names_of(text: str, resolver: AliasResolver | None, *, exclude: str = "") -> tuple[str, ...]:
    """从一张词表里取出命中的名字（去重、保序、排除被当成商品的那段）。"""
    if resolver is None:
        return ()
    names: dict[str, None] = {}
    for hit in resolver.scan(text):
        if hit.product is None:
            continue
        name = hit.product.display_name()
        if exclude and hit.product.name == exclude:
            continue
        names.setdefault(name, None)
    return tuple(names)


def _party_name(
    text: str,
    *,
    customers: AliasResolver | None,
    suppliers: AliasResolver | None,
    exclude: str = "",
) -> str | None:
    """认出客户或供应商名（客户优先：销售场景更常见）。"""
    for resolver in (customers, suppliers):
        if resolver is None:
            continue
        for hit in resolver.scan(text):
            if hit.product is not None and hit.product.name != exclude:
                return hit.product.display_name()
    return None


def _unknown_spans(
    text: str,
    spans: Mapping[str, SlotSpan],
    items: Sequence[ParsedItem],
) -> list[str]:
    """找出没能落到任何槽位的原话片段（用于提示「还有话没听懂」）。"""
    covered: list[str] = [span.text for span in spans.values() if span.text]
    covered.extend(item.text for item in items)
    remaining = text
    for piece in covered:
        remaining = remaining.replace(piece, " ")
    leftovers = [
        chunk.strip()
        for chunk in remaining.replace("，", " ").replace("。", " ").split()
        if len(chunk.strip()) >= 3
    ]
    return leftovers[:3]


def _route_for(spec: IntentSpec, *, clarify: ClarifyPrompt | None, confidence_value: float) -> str:
    """按契约与置信度决定路由（T04 据此决定出卡/只读/待办）。"""
    if clarify is not None:
        return "clarify"
    if spec.is_static:
        return "static"
    if spec.whitelist_blocked:
        return "pending_task"
    if spec.is_readonly:
        return "answer"
    if not should_card(confidence_value):
        return "clarify"
    return "card"


def _domain_hint(text: str) -> str | None:
    """未命中时给 G4 一个 domain 提示（减少 few-shot 取样范围）。"""
    if any(word in text for word in ("盘点", "库存", "还剩", "损耗", "报损")):
        return "stock"
    if any(word in text for word in ("卖", "出库", "客户", "退货")):
        return "sales"
    if any(word in text for word in ("进", "采购", "供应商", "供货")):
        return "purchase"
    if any(word in text for word in ("欠", "收", "付", "抹零")):
        return "receivable"
    if any(word in text for word in ("报表", "赚", "营业额")):
        return "report"
    if any(word in text for word in ("商品", "别名", "单价", "调价")):
        return "product"
    return None


def is_query(text: str) -> bool:
    """便捷判定：是否疑问句（对外暴露，供 pipeline 决定是否需要追问）。"""
    return is_interrogative(text)
