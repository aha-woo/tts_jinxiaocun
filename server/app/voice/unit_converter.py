"""单位换算（D3 铁律：**换算系数只能来自 unit_conversion，代码禁止臆造**）。

行为
----
- ``from_unit == to_unit`` → 直接通过（ratio=1，不教学）；
- 跨单位 → 只查 :class:`UnitRatioProvider`；
- **查不到 → 返回 ``ok=False`` + ``needs_teaching=True``**，卡片标「待教学」并进入教学队列
  （PRD 2.6 #3 兜底动作），**绝不用「常见换算」兜底**——1 箱是 12 瓶还是 24 瓶，
  猜错就是账错。

Provider 是可注入的：本轮用内存实现 + 未来接 ``unit_conversion`` 表（T04 落库读），
语义不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Protocol

from app.utils.unit import format_dual_unit

__all__ = [
    "UnitRatioProvider",
    "InMemoryRatioProvider",
    "UnitOutcome",
    "convert_with_ratios",
    "RatioKey",
]

#: 唯一合法来源 + 度量衡通用常数表（严禁把箱/件/瓶等包装换算写进这里）
STANDARD_WEIGHT_RATIOS: Mapping[tuple[str, str], Decimal] = {
    ("公斤", "斤"): Decimal("2"),
    ("斤", "公斤"): Decimal("0.5"),
    ("千克", "斤"): Decimal("2"),
    ("斤", "千克"): Decimal("0.5"),
    ("公斤", "千克"): Decimal("1"),
    ("千克", "公斤"): Decimal("1"),
    ("公斤", "克"): Decimal("1000"),
    ("克", "公斤"): Decimal("0.001"),
    ("千克", "克"): Decimal("1000"),
    ("克", "千克"): Decimal("0.001"),
    ("斤", "克"): Decimal("500"),
    ("克", "斤"): Decimal("0.002"),
    ("斤", "两"): Decimal("10"),
    ("两", "斤"): Decimal("0.1"),
    ("两", "克"): Decimal("50"),
    ("克", "两"): Decimal("0.02"),
}


def standard_weight_ratio(from_unit: str, to_unit: str) -> Decimal | None:
    """标准度量衡自动换算（公斤/千克/斤/两/克）；包装单位返回 ``None`` 强制待教学。"""
    return STANDARD_WEIGHT_RATIOS.get(((from_unit or "").strip(), (to_unit or "").strip()))


#: (product_id, from_unit, to_unit)
RatioKey = tuple[str, str, str]


class UnitRatioProvider(Protocol):
    """换算系数来源协议（唯一合法来源是商品档案的 ``unit_conversion``）。"""

    def ratio(self, product_id: str, from_unit: str, to_unit: str) -> float | None:
        """取 (商品, 原单位 -> 基本单位) 的系数；无记录必须返回 ``None``。"""


class InMemoryRatioProvider:
    """内存系数表（测试 / 尚未接 DB 时的实现）。"""

    def __init__(self, ratios: Mapping[RatioKey, float] | None = None) -> None:
        self._ratios: dict[RatioKey, float] = {
            key: float(value) for key, value in (ratios or {}).items()
        }

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str, str, float]]) -> "InMemoryRatioProvider":
        """从 ``(product_id, from_unit, to_unit, ratio)`` 行构造（对齐 ``unit_conversion`` 表）。"""
        return cls({(product_id, frm, to): ratio for product_id, frm, to, ratio in rows})

    def ratio(self, product_id: str, from_unit: str, to_unit: str) -> float | None:
        """查系数；不存在返回 ``None``（调用方必须走待教学，不得兜底）。"""
        value = self._ratios.get((product_id, from_unit, to_unit))
        return float(value) if value is not None else None


@dataclass(frozen=True)
class UnitOutcome:
    """换算结果（含「为什么没换算」的可解释字段）。"""

    ok: bool
    from_unit: str
    to_unit: str
    ratio: float | None
    qty_base: Decimal | None
    needs_teaching: bool
    reason: str
    trace: str

    @property
    def display(self) -> str:
        """双显文案：「原话 2 箱 = 48 瓶」；未换算时给出待教学提示。"""
        if not self.ok or self.ratio is None or self.qty_base is None:
            return f"{self.from_unit}→{self.to_unit} 缺换算系数（待教学）"
        return f"1 {self.from_unit} = {_fmt_ratio(self.ratio)} {self.to_unit}，原话数量已折算为 {_fmt_ratio(self.qty_base)} {self.to_unit}"


def _fmt_ratio(value: float | Decimal) -> str:
    """去掉多余的 ``.0``，便于展示。"""
    decimal = Decimal(str(value))
    normalized = decimal.normalize()
    return f"{normalized:f}"


def convert_with_ratios(
    quantity: Decimal | int | float,
    from_unit: str,
    to_unit: str,
    *,
    product_id: str,
    provider: UnitRatioProvider,
) -> UnitOutcome:
    """按档案系数把「原话数量」折算到 ``base_unit``。

    Args:
        quantity: 原话数量（Q）。
        from_unit: 原话单位（U）。
        to_unit: 商品基本单位（``product.base_unit``）。
        product_id: 商品 id（系数按商品隔离）。
        provider: 系数来源（``unit_conversion``）。

    Returns:
        :class:`UnitOutcome`；缺系数时 ``ok=False, needs_teaching=True``，**不臆造**。
    """
    qty = Decimal(str(quantity))
    if not from_unit or not to_unit:
        return UnitOutcome(False, from_unit, to_unit, None, None, False, "unit_missing", "缺少单位，无法折算")
    if from_unit == to_unit:
        return UnitOutcome(
            True,
            from_unit,
            to_unit,
            1.0,
            qty,
            False,
            "same_unit",
            f"原话单位与基本单位一致（{from_unit}），无需折算",
        )

    ratio = provider.ratio(product_id, from_unit, to_unit)
    if ratio is None or ratio <= 0:
        std_ratio = standard_weight_ratio(from_unit, to_unit)
        if std_ratio is not None:
            qty_base = (qty * std_ratio).quantize(Decimal("0.0001"))
            trace = f"{_fmt_ratio(qty)} {from_unit} × {_fmt_ratio(std_ratio)} = {_fmt_ratio(qty_base)} {to_unit}（标准换算）"
            return UnitOutcome(True, from_unit, to_unit, float(std_ratio), qty_base, False, "standard_weight", trace)
        # 关键：不给「常见值」兜底，宁可教学
        return UnitOutcome(
            False,
            from_unit,
            to_unit,
            None,
            None,
            True,
            "ratio_missing",
            f"{from_unit} 与 {to_unit} 之间没有已审核的换算系数（不得臆造）",
        )

    qty_base = (qty * Decimal(str(ratio))).quantize(Decimal("0.0001"))
    trace = f"{_fmt_ratio(qty)} {from_unit} × {_fmt_ratio(ratio)} = {_fmt_ratio(qty_base)} {to_unit}"
    return UnitOutcome(True, from_unit, to_unit, ratio, qty_base, False, "converted", trace)


def dual_unit_label(
    original_quantity: Decimal | int | float,
    original_unit: str,
    outcome: UnitOutcome,
) -> str:
    """双显标签（复用 T01 的 ``format_dual_unit``，保证端服同文案）。"""
    from app.utils.unit import ConversionResult

    return format_dual_unit(
        float(original_quantity),
        original_unit,
        ConversionResult(
            ok=outcome.ok,
            quantity=float(outcome.qty_base) if outcome.qty_base is not None else None,
            ratio=outcome.ratio,
            needs_teaching=outcome.needs_teaching,
            from_unit=outcome.from_unit,
            to_unit=outcome.to_unit,
        ),
    )
