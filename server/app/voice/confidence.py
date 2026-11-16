"""置信度合成与分档（G5 的判据，架构 §4 ⑤）。

分档口径（PRD 4.2，固定不变）
-----------------------------
====================  ==========================================
置信度区间             行为
====================  ==========================================
``>= 0.85``（high）    直出卡片，不需要额外追问
``0.6 ~ 0.85``（mid）  出卡片，但高亮需要核对的那一项
``< 0.6``（low）       **不出写卡**，只追问（避免「猜着记账」）
====================  ==========================================

合成为什么要「扣分项」而不是「加权平均」
----------------------------------------
加权平均会让「ASR 很自信」掩盖「槽位全是缺省值」这种危险组合。
因此这里用 **基线 - 明确扣分**：每个缺省值、每个 ``pending``、每个歧义、
每个阻断级冲突都有固定扣分，账算得清、也能在日志里逐项解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "BAND_HIGH",
    "BAND_MID",
    "BAND_LOW",
    "ConfidenceBreakdown",
    "band_of",
    "combine",
    "is_high",
    "should_ask",
    "should_card",
]

#: 直出阈值
BAND_HIGH = 0.85
#: 出卡 + 高亮阈值
BAND_MID = 0.6
#: 低于此值只追问
BAND_LOW = 0.6

#: 扣分表（每项都有明确理由，禁止魔法数字散落在业务代码里）
PENALTY_DEFAULT_FILL = 0.02  # 每个缺省槽位
PENALTY_DEFAULT_CAP = 0.10  # 缺省扣分封顶
PENALTY_PENDING = 0.12  # 「待补」「待教学」
PENALTY_AMBIGUITY = 0.25  # 数字/单位/商品歧义
PENALTY_BLOCKING_CONFLICT = 0.35  # 阻断级冲突（快照当增量等）
PENALTY_SOFT_CONFLICT = 0.06  # 非阻断冲突（老价、同音）
PENALTY_UNKNOWN_SPAN = 0.05  # 有没能落到槽位的原话片段


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """置信度明细（可解释、可日志、可断言）。"""

    base: float
    value: float
    penalties: tuple[str, ...] = ()
    bonuses: tuple[str, ...] = ()

    @property
    def band(self) -> str:
        """分档：``high`` / ``mid`` / ``low``。"""
        return band_of(self.value)

    def explain(self) -> str:
        """一行解释（日志用）。"""
        parts = [f"base={self.base:.2f}"]
        if self.penalties:
            parts.append("扣分: " + ", ".join(self.penalties))
        if self.bonuses:
            parts.append("加分: " + ", ".join(self.bonuses))
        parts.append(f"= {self.value:.2f} ({self.band})")
        return " | ".join(parts)


def _clamp(value: float) -> float:
    """夹到 ``[0, 1]``。"""
    return max(0.0, min(1.0, round(value, 4)))


def combine(
    *,
    rule_score: float | None = None,
    asr_confidence: float = 0.95,
    defaults: Iterable[str] = (),
    pending: Iterable[str] = (),
    ambiguities: Sequence[str] = (),
    blocking_conflicts: int = 0,
    soft_conflicts: int = 0,
    unknown_spans: Sequence[str] = (),
) -> ConfidenceBreakdown:
    """合成最终置信度。

    Args:
        rule_score: 规则层证据分（0~1）；LLM 层传模型自评或 ``None``。
        asr_confidence: ASR 置信度（同音纠正后已打过折）。
        defaults: 被缺省填上的槽位（每个都扣分，封顶 ``PENALTY_DEFAULT_CAP``）。
        pending: 标了 ``pending`` 的槽位。
        ambiguities: 歧义描述。
        blocking_conflicts: 阻断级冲突数。
        soft_conflicts: 非阻断冲突数。
        unknown_spans: 没能落到槽位的原话片段。

    Returns:
        :class:`ConfidenceBreakdown`
    """
    original_base = _clamp(rule_score if rule_score is not None else asr_confidence)
    # ASR 与规则层都不完美时取更低者，避免单边自信
    base = _clamp(min(original_base, asr_confidence))

    penalties: list[str] = []
    default_list = list(defaults)
    if default_list:
        # 一处扣 0.02，最多扣到 0.10（缺省是常态，不该让卡片直接降档）
        deducted = min(PENALTY_DEFAULT_CAP, PENALTY_DEFAULT_FILL * len(default_list))
        base -= deducted
        penalties.append(f"缺省填充 {len(default_list)} 项 -{deducted:.2f}")
    pending_list = list(pending)
    if pending_list:
        deducted = PENALTY_PENDING * min(len(pending_list), 2)
        base -= deducted
        penalties.append(f"pending {pending_list} -{deducted:.2f}")
    if ambiguities:
        deducted = PENALTY_AMBIGUITY * min(len(ambiguities), 2)
        base -= deducted
        penalties.append(f"歧义 {len(ambiguities)} 处 -{deducted:.2f}")
    if blocking_conflicts:
        deducted = PENALTY_BLOCKING_CONFLICT * blocking_conflicts
        base -= deducted
        penalties.append(f"阻断冲突 {blocking_conflicts} 处 -{deducted:.2f}")
    if soft_conflicts:
        deducted = PENALTY_SOFT_CONFLICT * soft_conflicts
        base -= deducted
        penalties.append(f"提示冲突 {soft_conflicts} 处 -{deducted:.2f}")
    if unknown_spans:
        deducted = PENALTY_UNKNOWN_SPAN * min(len(unknown_spans), 3)
        base -= deducted
        penalties.append(f"未落槽片段 {len(unknown_spans)} 处 -{deducted:.2f}")

    return ConfidenceBreakdown(base=original_base, value=_clamp(base), penalties=tuple(penalties))


def _parse_delta(penalty: str) -> float:
    """从扣分描述里还原增量（历史兼容，保留给外部日志解析）。"""
    try:
        return -float(penalty.rsplit("-", 1)[1])
    except (IndexError, ValueError):  # pragma: no cover - 文案格式由本模块保证
        return 0.0


def band_of(confidence: float) -> str:
    """把置信度映射到档位。"""
    if confidence >= BAND_HIGH:
        return "high"
    if confidence >= BAND_MID:
        return "mid"
    return "low"


def is_high(confidence: float) -> bool:
    """是否可直出（≥0.85）。"""
    return confidence >= BAND_HIGH


def should_card(confidence: float) -> bool:
    """是否允许出写卡（``>= BAND_MID`` 才出；低置信只追问）。"""
    return confidence >= BAND_MID


def should_ask(confidence: float) -> bool:
    """是否需要追问（与 ``should_card`` 互补）。"""
    return not should_card(confidence)
