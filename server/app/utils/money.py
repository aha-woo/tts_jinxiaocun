"""金额工具（服务端）——与 ``packages/shared/utils/money.ts`` 严格同语义。

铁律（架构 §8.4）
----------------
1. 金额一律以「分」为整数存储与传输（``*_fen``）；
2. 元 → 分使用 ``ROUND_HALF_UP``（远离零），禁止 ``float`` 乘 100；
3. 展示层才格式化，禁止浮点直接比较。

双端一致性由 ``packages/shared/utils/parity-cases.json`` + 两侧测试共同保证。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = [
    "yuan_to_fen",
    "fen_to_yuan",
    "fen_to_yuan_str",
    "format_fen",
    "parse_cny_text",
    "calc_trace",
]

_CENT = Decimal("0.01")
_CNY_UNIT_PATTERN = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)(?:块|元)(\d)?(?:毛|角)?$")
_BARE_NUMBER_PATTERN = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)$")


def yuan_to_fen(value: str | int | float | Decimal) -> int:
    """元 → 分（``ROUND_HALF_UP``，远离零）。

    Args:
        value: 元金额，推荐传 ``str`` 或 ``Decimal``；``float`` 会被 ``repr`` 后解析，
            调用方应自行确认精度来源。

    Returns:
        整数分。

    Raises:
        ValueError: 入参不是合法十进制数字。
    """
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError) as exc:
            raise ValueError(f"yuan_to_fen: 非法的金额输入 {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"yuan_to_fen: 非法的金额输入 {value!r}")
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return int(rounded * 100)


def fen_to_yuan(fen: int) -> float:
    """分 → 元（浮点，仅用于展示/图表；禁止用于计算或比较）。"""
    return fen / 100


def fen_to_yuan_str(fen: int) -> str:
    """分 → 元字符串（固定两位小数，不带符号）。

    Examples:
        >>> fen_to_yuan_str(1234)
        '12.34'
        >>> fen_to_yuan_str(5)
        '0.05'
    """
    if not isinstance(fen, int):
        raise ValueError(f"fen_to_yuan_str: 分必须为整数，收到 {fen!r}")
    negative = fen < 0
    absolute = abs(fen)
    body = f"{absolute // 100}.{absolute % 100:02d}"
    return f"-{body}" if negative else body


def format_fen(fen: int, symbol: str = "¥") -> str:
    """分 → 带货币符号的展示文案。

    Examples:
        >>> format_fen(1234)
        '¥12.34'
        >>> format_fen(-100)
        '-¥1.00'
    """
    text = fen_to_yuan_str(fen)
    if text.startswith("-"):
        return f"-{symbol}{text[1:]}"
    return f"{symbol}{text}"


def parse_cny_text(text: str) -> int | None:
    """解析中文金额口语（只处理「数字 + 块/元」，不做中文数字归一）。

    支持 ``3块5``、``3块5毛``、``12.5元``、``35``（裸数字按元处理）。
    纯中文数字（如「两百三」）返回 ``None`` —— 必须交给 T03 ``number_normalizer``。

    Args:
        text: 用户原话片段。

    Returns:
        整数分；无法解析返回 ``None``。
    """
    if not isinstance(text, str):
        return None
    trimmed = re.sub(r"\s+", "", text.strip())
    matched = _CNY_UNIT_PATTERN.match(trimmed)
    if matched is not None:
        sign, yuan_text, jiao_digit = matched.group(1), matched.group(2), matched.group(3)
        total = yuan_to_fen(yuan_text)
        if jiao_digit is not None:
            total += int(jiao_digit) * 10
        return -total if sign == "-" else total
    bare = _BARE_NUMBER_PATTERN.match(trimmed)
    if bare is not None:
        return yuan_to_fen(f"{bare.group(1)}{bare.group(2)}")
    return None


def calc_trace(quantity: int | float | Decimal, unit_price_fen: int, unit_label: str = "") -> str:
    """生成卡片上的计算式（如 ``2 箱 × ¥35.00 = ¥70.00``）。"""
    qty = Decimal(str(quantity))
    total = int((qty * Decimal(unit_price_fen)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    qty_text = f"{quantity} {unit_label}".strip() if unit_label else f"{quantity}"
    return f"{qty_text} × {format_fen(unit_price_fen)} = {format_fen(total)}"
