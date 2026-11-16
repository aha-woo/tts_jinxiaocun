"""金额与单价消歧 + 计算式（PRD 2.6 #4，H4「computed 必须带计算式」）。

判定规则（确定性关键词，**不用概率模型拍板**，D3）
--------------------------------------------------
| 原话形态 | 判定 |
|---|---|
| 「一件 / 每箱 / 一斤 / 单价 + 金额」 | ``UP``（单价） |
| 「一共 / 总共 / 合计 / 收了 / 付了 / 转了 + 金额」 | ``TA``（总价） |
| 「N 单位 + 金额」无「每」 | ``TA``，并**反算 UP**（卡片必须展示计算式让用户验算） |
| 只有一个裸数字、无线索 | **澄清**，不猜（PRD 2.6 #4 兜底动作） |

金额一律整数分；元→分用 ``Decimal`` + ``ROUND_HALF_UP``，**禁止 ``float * 100``**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.utils.money import calc_trace, fen_to_yuan_str, yuan_to_fen
from app.voice.number_normalizer import parse_money_yuan

__all__ = [
    "UP_HINTS",
    "TA_HINTS",
    "AmountOutcome",
    "compute_amounts",
    "find_money_text",
]

#: 单价线索（出现即判 ``UP``）
UP_HINTS: tuple[str, ...] = (
    "一件",
    "一箱",
    "一瓶",
    "一斤",
    "一个",
    "一条",
    "每件",
    "每箱",
    "每瓶",
    "每斤",
    "每个",
    "单价",
)

#: 总价线索（出现即判 ``TA``）
TA_HINTS: tuple[str, ...] = (
    "一共",
    "总共",
    "合计",
    "共计",
    "一共是",
    "收了",
    "付了",
    "转了",
    "给了",
    "花了",
)

#: 「金额出现在这些词之后」的定位（用于从整句里切出金额片段）
_MONEY_AFTER_HINT = re.compile(
    r"(?:一共|总共|合计|共计|收了|付了|转了|给了|花了|一件|一箱|一瓶|一斤|一个|一条|每件|每箱|每瓶|每斤|单价)"
    r"[^，,。；;！!？?]{0,6}$"
)

#: 金额片段（中文数字 + 块/元，或阿拉伯数字 + 块/元，或裸阿拉伯数字）
_MONEY_FRAGMENT = re.compile(
    r"[\d零〇一幺二两俩三仨四五六七八九勾拐十百千]+(?:[点\.][零〇一幺二两俩三仨四五六七八九勾拐]+)?[块元]"
    r"(?:[一二两三四五六七八九][毛角]?)?"
    r"|\d+(?:\.\d+)?"
)


@dataclass(frozen=True)
class AmountOutcome:
    """金额判定结果。"""

    up_fen: int | None = None
    ta_fen: int | None = None
    rule: str = "none"
    traces: tuple[str, ...] = field(default_factory=tuple)
    needs_clarify: bool = False
    confidence: float = 0.0
    money_text: str = ""
    qty: Decimal | None = None

    @property
    def matched(self) -> bool:
        """是否判出了至少一个金额槽位。"""
        return self.up_fen is not None or self.ta_fen is not None


def find_money_text(text: str) -> str:
    """从整句里切出「金额片段」（优先取线索词之后的第一个金额）。

    Examples:
        >>> find_money_text("今天进了两箱可乐，一件三十五")
        '三十五'
    """
    if not text:
        return ""
    best: tuple[int, str] = (-1, "")
    for match in _MONEY_FRAGMENT.finditer(text):
        token = match.group(0)
        # 纯阿拉伯数字必须带「块/元」或位于线索词之后，否则可能是数量（「20 件」）
        if token.isdigit() or re.fullmatch(r"\d+\.\d+", token):
            prefix = text[: match.start()]
            if not _MONEY_AFTER_HINT.search(prefix) and "块" not in text[match.end() : match.end() + 2]:
                continue
        best = (match.start(), token)
        break
    return best[1]


def _yuan_text_to_fen(number_text: str) -> int | None:
    """中文/阿拉伯混合金额 -> 整数分。"""
    parsed = parse_money_yuan(number_text)
    if parsed.value is None:
        return None
    yuan = str(parsed.value)
    return yuan_to_fen(Decimal(yuan))


def _reverse_unit_price(ta_fen: int, qty: Decimal) -> int:
    """由总价反算单价（``ROUND_HALF_UP``，禁止浮点）。"""
    unit_price = (Decimal(ta_fen) / qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(unit_price)


def compute_amounts(
    text: str,
    *,
    quantity: Decimal | None = None,
    unit_label: str = "",
) -> AmountOutcome:
    """从原话里判出 ``UP`` / ``TA``，并给出计算式。

    Args:
        text: 已归一化的原话（或片段）。
        quantity: 原话数量（Q，**原单位口径**，不是折算后的 base 数量）。
        unit_label: 原话单位（用于计算式文案）。

    Returns:
        :class:`AmountOutcome`；无法确定时 ``needs_clarify=True``。
    """
    money_text = find_money_text(text)
    if not money_text:
        return AmountOutcome(rule="no_money", qty=quantity)

    fen = _yuan_text_to_fen(money_text)
    if fen is None or fen <= 0:
        return AmountOutcome(rule="money_unparsed", money_text=money_text, qty=quantity, needs_clarify=True)

    has_up_hint = any(hint in text for hint in UP_HINTS)
    has_ta_hint = any(hint in text for hint in TA_HINTS)

    if has_up_hint and not has_ta_hint:
        traces = (f"单价：{money_text} = ¥{fen_to_yuan_str(fen)}/{unit_label or '件'}",)
        return AmountOutcome(up_fen=fen, rule="unit_price_hint", traces=traces, confidence=0.92,
                             money_text=money_text, qty=quantity)

    if has_ta_hint and not has_up_hint:
        traces = (f"总价：{money_text} = ¥{fen_to_yuan_str(fen)}",)
        outcome = AmountOutcome(ta_fen=fen, rule="total_hint", traces=traces, confidence=0.92,
                                money_text=money_text, qty=quantity)
        if quantity and quantity > 0:
            up_fen = _reverse_unit_price(fen, quantity)
            traces = traces + (calc_trace(quantity, up_fen, unit_label),)
            outcome = AmountOutcome(up_fen=up_fen, ta_fen=fen, rule="total_hint_reverse_up",
                                    traces=traces, confidence=0.9, money_text=money_text, qty=quantity)
        return outcome

    if has_up_hint and has_ta_hint:
        # 「一件三十五，一共两百三」两个都给了：先各自取，不做跨句反算
        return AmountOutcome(up_fen=fen, rule="both_hints", needs_clarify=True, money_text=money_text,
                             qty=quantity, confidence=0.5,
                             traces=(f"同时出现单价与总价线索，仅取到 ¥{fen_to_yuan_str(fen)}，需确认",))

    if quantity and quantity > 0:
        # 「N 单位 + 金额」无「每」 -> 总价，并反算单价（PRD 2.6 #4）
        up_fen = _reverse_unit_price(fen, quantity)
        traces = (
            f"总价：{money_text} = ¥{fen_to_yuan_str(fen)}",
            calc_trace(quantity, up_fen, unit_label),
        )
        return AmountOutcome(up_fen=up_fen, ta_fen=fen, rule="qty_plus_amount_total", traces=traces,
                             confidence=0.85, money_text=money_text, qty=quantity)

    # 只有一个裸数字、无线索 -> 澄清，不猜（「可乐 35」是单价还是总价？）
    return AmountOutcome(rule="bare_number_ambiguous", money_text=money_text, qty=quantity,
                         needs_clarify=True, confidence=0.3,
                         traces=(f"只听到金额 {money_text}，不知道是单价还是总价，需要问一句",))
