"""槽位填充：**缺省规则表** + 必填校验 + 金额自洽（G3/G4 共用的最后一步）。

缺省规则的口径必须唯一
----------------------
``slots.json`` 的 ``default_rule`` 是产品口径的单一真相源，本文件把它落成**代码表**
（:data:`DEFAULT_RULES`），一眼能看全「哪些槽位会被偷偷填上」：

===================  ==============================================  ============
槽位                 缺省行为                                         source
===================  ==============================================  ============
``D``                今天（业务发生日，租户时区）                    ``default``
``PS``               已结                                           ``default``
``W``                当前门店默认仓 / 主仓                           ``default``
``C``                散客（卡片**必须显示**，不得隐藏）               ``default``
``S``                **待补**（``pending``，禁止静默填值）            ``pending``
``U``                商品档案 ``base_unit``                         ``default``
``UP``               商品档案最近价                                  ``default``
``TA``               ``UP × Q`` 计算，必带 ``calc_trace``             ``computed``
``N``                空（不填，避免污染备注）                        —
``P/Q/AMT/DOC/TF/TV/EF/ALIAS/RATIO``  **无缺省**，缺就追问          —
===================  ==============================================  ============

「宁可问一句，不可猜一个」：任何 ``pending`` 槽位都会让卡片带「待补」标记，
不允许被缺省值伪装成已确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from app.domain.intent_registry import IntentSpec
from app.utils.money import ROUND_HALF_UP, calc_trace, fen_to_yuan_str
from app.voice.alias_resolver import ProductEntry
from app.voice.template_rule import SlotSpan
from app.voice.unit_converter import InMemoryRatioProvider, convert_with_ratios, dual_unit_label

__all__ = [
    "DEFAULT_RULES",
    "DefaultRule",
    "SlotFillResult",
    "SlotFiller",
    "ZERO_ALLOWED_INTENTS",
]

#: 允许数量为 0 的意图（盘点/报损：0 是有效快照）
ZERO_ALLOWED_INTENTS: frozenset[str] = frozenset(
    {"stock.take", "stock.take_multi", "stock.loss"}
)

#: 付款状态未结会触发风险升级的意图（PRD 2.5 动态升级）
_PAYMENT_ESCALATION_INTENTS: frozenset[str] = frozenset(
    {"purchase.create", "purchase.create_multi", "sales.create", "sales.create_multi"}
)


@dataclass(frozen=True)
class DefaultRule:
    """一条缺省规则（``strategy`` 决定值怎么来）。"""

    code: str
    strategy: str
    value: Any = None
    source: str = "default"
    note: str = ""


#: 槽位缺省规则表（口径来自 ``slots.json``；改这里必须同步改契约注释）
DEFAULT_RULES: Mapping[str, DefaultRule] = {
    "D": DefaultRule("D", "today", source="default", note="业务发生日=今天"),
    "PS": DefaultRule("PS", "const", value="已结", source="default", note="未说付款状态按已结"),
    "W": DefaultRule("W", "store", source="default", note="当前门店默认仓"),
    "C": DefaultRule("C", "const", value="散客", source="default", note="卡片必须显示「散客」"),
    "S": DefaultRule("S", "const", value="待补", source="pending", note="禁止静默填供应商"),
    "U": DefaultRule("U", "product_unit", source="default", note="商品基本单位"),
    "UP": DefaultRule("UP", "product_price", source="default", note="商品档案最近价"),
}


@dataclass
class SlotFillResult:
    """填充结果（含解释信息，便于日志与测试断言）。"""

    slots: dict[str, Any] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)
    traces: list[str] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    escalations: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)

    def add(self, code: str, value: Any, source: str) -> None:
        """写入一个槽位值与其来源。"""
        self.slots[code] = value
        self.source[code] = source


class SlotFiller:
    """按契约缺省规则把片段补成一套完整槽位。"""

    def __init__(self, *, store_name: str = "主仓") -> None:
        self._store_name = store_name

    def fill(
        self,
        spec: IntentSpec,
        spans: Mapping[str, SlotSpan],
        *,
        today_iso: str,
        product: ProductEntry | None = None,
        partner_name: str | None = None,
        last_date_iso: str | None = None,
    ) -> SlotFillResult:
        """把已抽取的片段 + 缺省规则合成槽位集合。

        Args:
            spec: 目标意图规格。
            spans: ``{槽位代号: 片段}``（引擎从原话抽出）。
            today_iso: 今天（``YYYY-MM-DD``）。
            product: 命中的商品档案（供 ``U`` / ``UP`` 缺省与单位换算展示）。
            partner_name: 命中的客户/供应商名（``C`` / ``S``）。
            last_date_iso: 最近一单日期（``D`` 的「上次」锚点）。

        Returns:
            :class:`SlotFillResult`
        """
        result = SlotFillResult()

        # 1) 原话片段优先
        for code, span in spans.items():
            if span.value is None:
                if span.ambiguous:
                    result.ambiguities.append(
                        f"{code}：「{span.text}」有 {len(span.alternatives)} 种读法"
                        f"（{' / '.join(span.alternatives)}），需要问一句"
                    )
                continue
            result.add(code, span.value, span.source)
            if span.note:
                result.traces.append(f"{code}：{span.note}")

        # 2) 客户 / 供应商（只填该 intent 声明过的槽位）
        if partner_name:
            if "C" in spec.all_slots and "C" not in result.slots:
                result.add("C", partner_name, "asr")
            elif "S" in spec.all_slots and "S" not in result.slots:
                result.add("S", partner_name, "asr")

        # 3) 缺省规则
        for code in spec.all_slots:
            if code in result.slots:
                continue
            rule = DEFAULT_RULES.get(code)
            if rule is None:
                continue
            value, source = self._resolve_default(
                rule,
                today_iso=today_iso,
                product=product,
                spec=spec,
                last_date_iso=last_date_iso,
            )
            if value is None:
                continue
            result.add(code, value, source)
            if rule.note:
                result.defaults[code] = rule.note

        # 4) 数量合法性 + 单位换算展示
        self._check_quantity(spec, result)
        self._annotate_unit_conversion(result, product)

        # 5) 金额自洽（UP × Q ↔ TA）
        self._finalize_amounts(spec, result)

        # 6) 付款状态升级 + 必填校验
        self._annotate_escalation(spec, result)
        result.missing_required = tuple(
            code for code in spec.required_slots if code not in result.slots
        )
        result.pending = tuple(
            sorted(code for code, src in result.source.items() if src == "pending")
        )
        return result

    # ------------------------------------------------------------------ 内部
    def _resolve_default(
        self,
        rule: DefaultRule,
        *,
        today_iso: str,
        product: ProductEntry | None,
        spec: IntentSpec,
        last_date_iso: str | None,
    ) -> tuple[Any, str]:
        """按策略解析缺省值；解析不出返回 ``(None, source)``（不硬填）。"""
        del spec, last_date_iso  # 保留参数：未来「上次」类缺省会用到
        if rule.strategy == "today":
            return today_iso, rule.source
        if rule.strategy == "const":
            return rule.value, rule.source
        if rule.strategy == "store":
            return self._store_name, rule.source
        if rule.strategy == "product_unit":
            return (product.base_unit if product is not None else None), rule.source
        if rule.strategy == "product_price":
            if product is None:
                return None, rule.source
            price = product.last_purchase_price_fen
            if price is None:
                price = product.last_sale_price_fen
            return (price if price and price > 0 else None), rule.source
        return None, rule.source

    def _check_quantity(self, spec: IntentSpec, result: SlotFillResult) -> None:
        """数量校验：盘点/损耗允许 0，其余必须 > 0。"""
        if "Q" not in result.slots:
            return
        value = result.slots["Q"]
        if not isinstance(value, (int, float)):
            return
        allow_zero = spec.intent_id in ZERO_ALLOWED_INTENTS
        if value < 0:
            result.traces.append(f"Q：数量 {value} 非法（负数请用「撤销」）")
            del result.slots["Q"]
            del result.source["Q"]
            return
        if value == 0 and not allow_zero:
            result.traces.append(f"Q：{spec.intent_id} 的数量必须大于 0")
            del result.slots["Q"]
            del result.source["Q"]

    def _annotate_unit_conversion(
        self, result: SlotFillResult, product: ProductEntry | None
    ) -> None:
        """有换算系数时给双显文案；**没有系数绝不臆造**（PRD 2.6 #3）。"""
        if product is None or "Q" not in result.slots or "U" not in result.slots:
            return
        quantity = result.slots["Q"]
        unit = str(result.slots["U"])
        if unit == product.base_unit:
            return
        provider = InMemoryRatioProvider(
            {
                (product.product_id, unit, product.base_unit): float(ratio)
                for ratio_unit, ratio in (product.ratios or {}).items()
                if ratio_unit == unit
            }
        )
        outcome = convert_with_ratios(
            quantity,
            unit,
            product.base_unit,
            product_id=product.product_id,
            provider=provider,
        )
        if not outcome.ok:
            # 没有已审核系数：**绝不臆造**，标待教学并让卡片显式提示
            result.traces.append(f"U：{outcome.trace}，标待教学")
            result.source["U"] = "pending"
            return
        result.traces.append(dual_unit_label(quantity, unit, outcome))

    def _finalize_amounts(self, spec: IntentSpec, result: SlotFillResult) -> None:
        """金额自洽：``TA = UP × Q``（``ROUND_HALF_UP``），反向也支持。"""
        del spec
        up = result.slots.get("UP")
        ta = result.slots.get("TA")
        qty = result.slots.get("Q")
        unit = str(result.slots.get("U") or "")

        if isinstance(up, int) and up > 0 and isinstance(qty, (int, float)) and qty > 0 and ta is None:
            computed = int((Decimal(up) * Decimal(str(qty))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            result.add("TA", computed, "computed")
            result.traces.append(calc_trace(qty, up, unit))
            return

        if isinstance(ta, int) and ta > 0 and isinstance(qty, (int, float)) and qty > 0 and up is None:
            price = int((Decimal(ta) / Decimal(str(qty))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if price > 0:
                result.add("UP", price, "computed")
                result.traces.append(
                    f"单价反算：{fen_to_yuan_str(ta)} ÷ {qty} {unit or '件'} = {fen_to_yuan_str(price)}"
                )
            return

        if isinstance(up, int) and isinstance(ta, int) and isinstance(qty, (int, float)) and qty > 0:
            expected = int((Decimal(up) * Decimal(str(qty))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if expected != ta:
                result.traces.append(
                    f"金额不自洽：{qty} {unit or '件'} × {fen_to_yuan_str(up)} = "
                    f"{fen_to_yuan_str(expected)}，与总价 {fen_to_yuan_str(ta)} 不一致"
                )

    def _annotate_escalation(self, spec: IntentSpec, result: SlotFillResult) -> None:
        """``PS=未结`` 触发风险升级（只标注，不改契约）。"""
        if spec.intent_id not in _PAYMENT_ESCALATION_INTENTS:
            return
        if result.slots.get("PS") == "未结":
            result.escalations.append(
                f"{spec.intent_id}：PS=未结 → 升 R2/C2（赊账必须二次确认）"
            )
        elif result.slots.get("PS") == "部分":
            result.escalations.append(f"{spec.intent_id}：PS=部分 → 需显示已付/未付金额")


