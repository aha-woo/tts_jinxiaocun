"""中文数字归一（PRD 2.6 #8 / H15）—— 只做「词 -> 数字」，不含任何业务规则。

覆盖
----
- 口语/军语读法：幺=1、拐=7、勾=9、两/俩=2、仨=3；
- 数量级：十/百/千/万，含口语省尾（「一百二」=120、「两百三」=230、「一万二」=12000）；
- 阿拉伯数字与小数（``3.5``、``12``）；
- 元角口语（「两块五」=2.5 元、「三块五毛」）——**只返回「元」的 Decimal**，
  转分由 :mod:`app.voice.amount_calculator` 用 ``ROUND_HALF_UP`` 完成；
- **歧义不猜**：连续裸数字（「三五」可能是 35 / 3.5 / 3 和 5）返回 ``value=None``
  + ``alternatives``，让 G5 去追问（PRD 2.6 #8 兜底动作）。

为什么不做上下文推断
--------------------
「三五」的正确值只能由语境决定；在这里猜一次，账就错一次。宁可追问一轮。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = [
    "CN_DIGITS",
    "CN_UNITS",
    "NumberParse",
    "parse_quantity",
    "parse_money_yuan",
    "parse_integer_text",
    "find_number_token",
]

#: 数字词（含口语/军语读法；「是否纳入军语」按 PRD 标注为需核实）
CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "幺": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "贰": 2,
    "三": 3,
    "仨": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "拐": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "勾": 9,
    "玖": 9,
}

#: 量级词
CN_UNITS: dict[str, int] = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "万": 10000}

#: 「块/元」后接角位数字（「两块五」= 2.5 元）
_YUAN_JIAO_PATTERN = re.compile(r"(?P<yuan>[\d零〇一幺二两俩三仨四五六七八九勾拐十百千]+)[块元](?P<jiao>[一二两三四五六七八九])?(?:毛|角)?")

#: 纯阿拉伯数字（含小数）
_ARABIC_PATTERN = re.compile(r"\d+(?:\.\d+)?")

#: 连续中文数字片段
_CN_RUN_PATTERN = re.compile(r"[零〇一幺二两俩三仨四五六七八九勾拐十百千万]+")

#: 中文小数（「三点五」）
_CN_DECIMAL_PATTERN = re.compile(r"(?P<int>[零〇一幺二两俩三仨四五六七八九勾拐十百千]+)[点\.](?P<frac>[零〇一幺二两俩三仨四五六七八九勾拐]+)")


@dataclass(frozen=True)
class NumberParse:
    """数字解析结果。"""

    value: Decimal | None
    raw: str
    matched: bool
    needs_confirm: bool
    alternatives: tuple[str, ...]
    trace: str
    confidence: float

    @property
    def ok(self) -> bool:
        """是否解析出唯一确定值。"""
        return self.value is not None


def parse_integer_text(text: str) -> int | None:
    """把纯中文整数文本转成 int（支持口语省尾）。

    Examples:
        >>> parse_integer_text("三十五")
        35
        >>> parse_integer_text("一百二")   # 口语省尾 -> 120
        120
        >>> parse_integer_text("一万二")
        12000
    """
    if not text:
        return None
    total = 0
    section = 0
    number = 0
    last_unit = 0
    for char in text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_UNITS:
            unit = CN_UNITS[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1  # 「十五」= 15
                section += number * unit
                number = 0
            last_unit = unit
        else:
            return None
    if number:
        # 「一百二」= 120；「一万二」= 12000；「三十五」= 35
        section += number * (last_unit // 10) if last_unit >= 100 else number
    return total + section


def find_number_token(text: str) -> tuple[str, str] | None:
    """在文本里定位第一个数字片段。

    Returns:
        ``(片段文本, 类型)``，类型 ∈ ``{"arabic", "cn_yuan_jiao", "cn_decimal", "cn"}``；
        找不到返回 ``None``。
    """
    if not text:
        return None
    candidates: list[tuple[int, str, str]] = []
    for pattern, kind in (
        (_YUAN_JIAO_PATTERN, "cn_yuan_jiao"),
        (_CN_DECIMAL_PATTERN, "cn_decimal"),
        (_ARABIC_PATTERN, "arabic"),
        (_CN_RUN_PATTERN, "cn"),
    ):
        match = pattern.search(text)
        if match is not None:
            candidates.append((match.start(), match.group(0), kind))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -len(item[1])))
    _, token, kind = candidates[0]
    return token, kind


def _is_ambiguous_cn_run(token: str) -> bool:
    """连续裸数字（无「十百千万」）且长度 ≥2 → 无法唯一定值（「三五」）。"""
    if len(token) < 2:
        return False
    return not any(char in CN_UNITS for char in token)


def parse_quantity(text: str) -> NumberParse:
    """解析「数量」类数字（Q / RATIO），不做任何单位推断。

    Args:
        text: 原话片段。

    Returns:
        :class:`NumberParse`；歧义时 ``value=None`` 且 ``alternatives`` 非空。
    """
    found = find_number_token(text)
    if found is None:
        return NumberParse(None, "", False, False, (), "", 0.0)
    token, kind = found

    if kind == "arabic":
        try:
            value = Decimal(token)
        except InvalidOperation:  # pragma: no cover - 正则已保证
            return NumberParse(None, token, False, False, (), "", 0.0)
        needs_confirm = value != value.to_integral_value()
        return NumberParse(value, token, True, needs_confirm, (), f"{token} = {value}", 0.95 if not needs_confirm else 0.7)

    if kind == "cn_decimal":
        match = _CN_DECIMAL_PATTERN.match(token)
        assert match is not None
        integer = parse_integer_text(match.group("int"))
        frac_digits = "".join(str(CN_DIGITS[char]) for char in match.group("frac"))
        if integer is None:
            return NumberParse(None, token, False, False, (), "", 0.0)
        value = Decimal(f"{integer}.{frac_digits}")
        return NumberParse(value, token, True, False, (), f"{token} = {value}", 0.9)

    if kind == "cn_yuan_jiao":
        match = _YUAN_JIAO_PATTERN.match(token)
        assert match is not None
        yuan = parse_integer_text(match.group("yuan"))
        if yuan is None:
            return NumberParse(None, token, False, False, (), "", 0.0)
        jiao = match.group("jiao")
        value = Decimal(yuan)
        if jiao:
            value = value + Decimal(CN_DIGITS[jiao]) / Decimal(10)
        return NumberParse(value, token, True, False, (), f"{token} = {value}", 0.9)

    if _is_ambiguous_cn_run(token):
        # 「三五」：35 / 3.5 / 3 和 5 —— 不猜，交给 G5 追问（PRD 2.6 #8）
        digits = "".join(str(CN_DIGITS[char]) for char in token)
        alternatives = (digits, f"{digits[0]}.{digits[1:]}", "、".join(digits))
        return NumberParse(None, token, True, True, alternatives, "", 0.2)

    integer = parse_integer_text(token)
    if integer is None:
        return NumberParse(None, token, False, False, (), "", 0.0)
    # 「一百二」「两百三」这类口语省尾必须高亮让用户核对（PRD 2.6 #8）
    colloquial = bool(re.search(r"[百千万][一二两三四五六七八九]$", token))
    return NumberParse(
        Decimal(integer),
        token,
        True,
        colloquial,
        (),
        f"{token} = {integer}",
        0.85 if colloquial else 0.95,
    )


def parse_money_yuan(text: str) -> NumberParse:
    """解析「金额」类数字，返回**元**的 Decimal（转分由 amount_calculator 负责）。

    「元角口语」优先（「两块五」= 2.5 元），其次中文整数（「两百三」= 230 元），
    最后阿拉伯数字（「12」= 12 元）。
    """
    if not text:
        return NumberParse(None, "", False, False, (), "", 0.0)
    match = _YUAN_JIAO_PATTERN.search(text)
    if match is not None:
        return parse_quantity(match.group(0))
    match = _CN_DECIMAL_PATTERN.search(text)
    if match is not None:
        return parse_quantity(match.group(0))
    match = _ARABIC_PATTERN.search(text)
    if match is not None:
        yuan = Decimal(match.group(0))
        return NumberParse(yuan, match.group(0), True, False, (), f"{match.group(0)} 元", 0.95)
    match = _CN_RUN_PATTERN.search(text)
    if match is not None:
        return parse_quantity(match.group(0))
    return NumberParse(None, "", False, False, (), "", 0.0)
