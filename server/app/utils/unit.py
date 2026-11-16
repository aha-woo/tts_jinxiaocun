"""单位归一工具（服务端）——与 ``packages/shared/utils/unit.ts`` 严格同语义。

铁律（架构 §8.4）
----------------
1. 一律折算到商品 ``base_unit``，并**双显**「原话 2 箱 = 48 瓶」；
2. 换算系数只能来自 ``unit_conversion``（需审核），**代码禁止臆造系数**；
3. 无系数 -> ``ok=False`` + ``needs_teaching=True``（卡片标「待教学」，进入教学队列）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "CANONICAL_UNITS",
    "SPELLING_VARIANTS",
    "ConversionResult",
    "normalize_unit",
    "convert_quantity",
    "format_dual_unit",
]

#: 已登记的标准单位（纯单位名归一，不含任何换算含义）
CANONICAL_UNITS: tuple[str, ...] = (
    "箱",
    "件",
    "瓶",
    "斤",
    "个",
    "条",
    "罐",
    "包",
    "桶",
    "提",
    "袋",
    "盒",
)

#: 仅处理「拼写变体」的同义写法，**绝不**跨量纲映射（公斤 ≠ 斤）
SPELLING_VARIANTS: Mapping[str, str] = {
    "整箱": "箱",
    "箱装": "箱",
    "件装": "件",
    "整件": "件",
    "瓶装": "瓶",
    "条装": "条",
    "罐装": "罐",
    "袋装": "袋",
    "盒装": "盒",
    "包装": "包",
}

PUNCTUATION_PATTERN = re.compile(r"[，,。\.!！?？]")


@dataclass(frozen=True)
class ConversionResult:
    """换算结果。"""

    ok: bool
    quantity: float | None
    ratio: float | None
    needs_teaching: bool
    from_unit: str
    to_unit: str


def normalize_unit(text: str) -> str | None:
    """单位名归一：去掉空白/标点与口语后缀（「装」「儿」）。

    Args:
        text: 原话单位片段。

    Returns:
        标准单位；无法识别返回 ``None``（调用方必须澄清，不得猜）。
    """
    if not isinstance(text, str):
        return None
    normalized = PUNCTUATION_PATTERN.sub("", text.strip()).replace(" ", "")
    if normalized == "":
        return None
    if normalized in SPELLING_VARIANTS:
        return SPELLING_VARIANTS[normalized]
    while normalized.endswith(("儿", "装")) and len(normalized) > 1:
        normalized = normalized[:-1]
        if normalized in SPELLING_VARIANTS:
            return SPELLING_VARIANTS[normalized]
    if normalized in CANONICAL_UNITS:
        return normalized
    return None


def _round_to_6(value: float) -> float:
    """保留 6 位小数，避免 1/24 之类产生浮点尾差（与 TS 侧 Math.round(x*1e6)/1e6 对齐）。"""
    return round(value * 1_000_000) / 1_000_000


def convert_quantity(
    quantity: float,
    from_unit: str,
    to_unit: str,
    conversions: Mapping[str, Mapping[str, float]] | None = None,
) -> ConversionResult:
    """数量单位换算。

    Args:
        quantity: 原话数量（必须 >= 0）。
        from_unit: 原话单位（应为 :func:`normalize_unit` 的结果）。
        to_unit: 目标单位（商品 ``base_unit``）。
        conversions: 换算表，形如 ``{"箱": {"瓶": 24}}`` 表示 1 箱 = 24 瓶；
            缺失即视为无系数，**不得臆造**。

    Returns:
        :class:`ConversionResult`
    """
    table: Mapping[str, Mapping[str, float]] = conversions or {}
    failure = ConversionResult(
        ok=False,
        quantity=None,
        ratio=None,
        needs_teaching=True,
        from_unit=from_unit,
        to_unit=to_unit,
    )
    if quantity is None or quantity < 0:
        return failure
    if from_unit == to_unit:
        return ConversionResult(
            ok=True,
            quantity=float(quantity),
            ratio=1.0,
            needs_teaching=False,
            from_unit=from_unit,
            to_unit=to_unit,
        )

    direct = (table.get(from_unit) or {}).get(to_unit)
    if isinstance(direct, (int, float)) and direct > 0:
        return ConversionResult(
            ok=True,
            quantity=_round_to_6(quantity * float(direct)),
            ratio=float(direct),
            needs_teaching=False,
            from_unit=from_unit,
            to_unit=to_unit,
        )

    inverse = (table.get(to_unit) or {}).get(from_unit)
    if isinstance(inverse, (int, float)) and inverse > 0:
        return ConversionResult(
            ok=True,
            quantity=_round_to_6(quantity / float(inverse)),
            ratio=_round_to_6(1 / float(inverse)),
            needs_teaching=False,
            from_unit=from_unit,
            to_unit=to_unit,
        )

    # 无系数：绝不臆造，交回澄清/教学
    return failure


def format_dual_unit(original_quantity: float, original_unit: str, result: ConversionResult) -> str:
    """双显文案：「2 箱 = 48 瓶」；无系数时返回待教学文案。"""
    if result.ok and result.quantity is not None:
        quantity = result.quantity
        quantity_text = f"{quantity:g}"
        return f"{original_quantity:g} {original_unit} = {quantity_text} {result.to_unit}"
    return f"{original_quantity:g} {original_unit}（{result.to_unit} 换算待教）"
