"""ASR 结果校准与置信度分档（H15 / PRD 4.2）。

两件事
------
1. **同音词纠偏**（PRD 2.6 #11）：只在**上下文能确定**时替换，且替换后可追溯；
   回显一律用业务词（不把「帐/香/见/推」这类错字丢给用户看）。
2. **置信度分档**：``≥0.85`` 直出 / ``0.6~0.85`` 出卡+高亮追问 / ``<0.6`` 不出写卡只追问。

为什么纠偏要带上下文
--------------------
「香」在「香水」里是正确的，只有「两香」才几乎必然是「两箱」。无条件替换会改坏
本来正确的文本，所以规则全部写成「数字后 + 特定字」这种高确定度形态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "HIGH_CONFIDENCE",
    "MID_CONFIDENCE",
    "HOMOPHONE_RULES",
    "CalibrationResult",
    "band_of",
    "calibrate_text",
    "adjust_confidence",
    "is_high",
    "is_low",
]

#: 高置信阈（≥ 直出）
HIGH_CONFIDENCE: float = 0.85
#: 中置信下界（0.6~0.85 出卡+高亮追问）
MID_CONFIDENCE: float = 0.6

#: 数字词（用于「数字后 + 同音字」上下文判定）
_NUMERAL_CLASS = r"[零〇一幺二两俩三仨四五六七八九勾拐十百千万\d]"

#: 常见口语起手废话（只剥离句首填充词，避免干扰主干解析）
_FILLER_PREFIX_RE = re.compile(
    r"^(?:(?:嗯|呃|啊|那个|就是|帮我记一下|帮我记个|麻烦记一下|记一下|对了对了|对了)[，,、\s]*)+",
)

#: 「香」后接这些字时属于商品名词（如五香花生、十三香、香水、香烟），禁止误改「箱」
_XIANG_COMPOUND_TAIL = r"[花粉末肉牛豆蛋瓜肠菇油烟水味干脆酥料卤]"

#: 同音纠偏规则：(pattern, replacement, 说明)
HOMOPHONE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"帐"), "账", "帐/账 统一为「账」"),
    (
        re.compile(rf"(?<!十三)(?<={_NUMERAL_CLASS})香(?!{_XIANG_COMPOUND_TAIL})"),
        "箱",
        "数字后的「香」= 箱",
    ),
    (re.compile(rf"(?<={_NUMERAL_CLASS})见"), "件", "数字后的「见」= 件"),
    (re.compile(rf"(?<={_NUMERAL_CLASS})推"), "退", "数字后的「推」= 退"),
    (re.compile(rf"(?<={_NUMERAL_CLASS})勾"), "9", "军语「勾」= 9（数值语境）"),
)


@dataclass(frozen=True)
class CalibrationResult:
    """校准结果。"""

    text: str
    original: str
    corrections: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """是否发生过纠偏。"""
        return bool(self.corrections)


def calibrate_text(
    text: str,
    *,
    personal_fixes: Mapping[str, str] | None = None,
) -> CalibrationResult:
    """按上下文安全的规则纠偏同音词、剥离口语废话，并应用个人口癖替换。"""
    original = text or ""
    current = original.strip()
    corrections: list[str] = []

    if personal_fixes:
        for wrong, right in sorted(personal_fixes.items(), key=lambda item: len(item[0]), reverse=True):
            if wrong and right and wrong != right and wrong in current:
                current = current.replace(wrong, right)
                corrections.append(f"个人词表「{wrong}→{right}」")

    stripped = _FILLER_PREFIX_RE.sub("", current).strip()
    if stripped and len(stripped) >= 2 and stripped != current:
        current = stripped

    for pattern, replacement, note in HOMOPHONE_RULES:
        replaced, count = pattern.subn(replacement, current)
        if count:
            corrections.append(f"{note}（{count} 处）")
            current = replaced
    return CalibrationResult(text=current, original=original, corrections=tuple(corrections))


def adjust_confidence(raw: float, *, corrections: int = 0) -> float:
    """按纠偏次数下调置信度（每处 -0.05，最多 -0.15；不下调到底于 0）。"""
    penalty = min(0.15, 0.05 * max(0, corrections))
    return max(0.0, min(1.0, float(raw) - penalty))


def band_of(confidence: float) -> str:
    """置信度分档：``high`` / ``mid`` / ``low``。"""
    value = float(confidence)
    if value >= HIGH_CONFIDENCE:
        return "high"
    if value >= MID_CONFIDENCE:
        return "mid"
    return "low"


def is_high(confidence: float) -> bool:
    """是否可直出卡片。"""
    return float(confidence) >= HIGH_CONFIDENCE


def is_low(confidence: float) -> bool:
    """是否低于 0.6（不出写卡，只追问）。"""
    return float(confidence) < MID_CONFIDENCE
