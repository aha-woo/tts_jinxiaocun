"""G5 追问话术：**全部静态内容，一个字都不调 AI**（PRD 4.3 / 架构 §4 ⑤）。

硬约束（团队已拍）
------------------
1. **一次只问 1 个槽位**（``ClarifyPrompt.slot`` 单值）；
2. 选项 **2~3 个**可点（少到 2、多到 3，多一个都不给）；
3. 话术来自契约（``intents.json`` 每个 intent 的 ``clarifier_templates``，
   缺失时回落到 ``slots.json`` 的 ``clarifier_template``），**不在这里重新发明**；
4. 用户点选后只做**本地结构化更新**（见 ``clarify_fsm``），不重调 AI。

本模块只负责「选哪个槽位问 + 话术怎么拼 + 选项给什么」，
不持有状态（状态在 ``clarify_fsm``），不发网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from app.domain.intent_registry import IntentRegistry, IntentSpec, default_registry
from app.voice.output_schema import ClarifyPrompt

__all__ = [
    "DEFAULT_OPTIONS",
    "MAX_OPTIONS",
    "MIN_OPTIONS",
    "ClarifierCatalog",
    "build_clarify",
    "default_catalog",
    "pick_slot_to_ask",
]

#: 选项条数硬约束（G5）
MIN_OPTIONS = 2
MAX_OPTIONS = 3

#: 每个槽位的**静态**可点选项（业务上就那么几种，不需要 AI 生成）
DEFAULT_OPTIONS: Mapping[str, tuple[str, ...]] = {
    "PS": ("已结", "未结", "部分"),
    "U": ("箱", "件", "瓶"),
    "D": ("今天", "昨天", "前天"),
    "Q": ("1", "2", "10"),
    "C": ("散客", "老客户", "新客户"),
    "S": ("待补", "现金进货", "上月同一家"),
    "W": ("主仓", "门店", "分仓"),
    "AMT": ("按单据金额", "按欠款全额", "部分金额"),
    "DOC": ("上一单", "昨天那张", "刚才那张"),
    "TF": ("进价", "售价", "备注"),
    "RATIO": ("1 箱 = 24 瓶", "1 箱 = 12 瓶", "1 件 = 1 瓶"),
    "ALIAS": ("按原话记", "再教一次", "跳过"),
    "P": ("按上次那个", "另说一个名字", "取消"),
}

#: 追问优先级：先问最影响入账的（数量 → 商品 → 单位 → 钱）
_ASK_ORDER: tuple[str, ...] = (
    "P",
    "Q",
    "U",
    "TA",
    "UP",
    "AMT",
    "C",
    "S",
    "TF",
    "TV",
    "D",
    "PS",
    "W",
    "DOC",
    "RATIO",
    "ALIAS",
    "EF",
    "N",
)


@dataclass(frozen=True)
class ClarifierCatalog:
    """追问话术目录（平台级静态内容 + 槽位兜底话术）。"""

    #: ``{(intent_id, slot_code): 话术}``
    per_intent: Mapping[tuple[str, str], str]
    #: ``{slot_code: 话术}``（``slots.json`` 的兜底）
    per_slot: Mapping[str, str]

    def question_for(self, intent_id: str, slot_code: str) -> str:
        """取某 intent 某槽位的话术；缺失时逐级兜底（**永不返回空串**）。"""
        direct = self.per_intent.get((intent_id, slot_code))
        if direct:
            return direct
        generic = self.per_slot.get(slot_code)
        if generic:
            return generic
        return f"再说一下「{slot_code}」这一项？"

    def options_for(self, intent_id: str, slot_code: str) -> tuple[str, ...]:
        """取该槽位的静态选项（保证 2~3 个）。"""
        del intent_id  # 选项与 intent 无关，保留参数便于将来按意图细分
        pool: Sequence[str] = DEFAULT_OPTIONS.get(slot_code, ("是", "不是", "跳过"))
        return _clip(pool)


def _clip(options: Iterable[str]) -> tuple[str, ...]:
    """裁剪到 2~3 个并去重保序（G5 硬约束）。"""
    seen: dict[str, None] = {}
    for option in options:
        text = (option or "").strip()
        if text:
            seen.setdefault(text, None)
        if len(seen) >= MAX_OPTIONS:
            break
    result = tuple(seen)
    if len(result) >= MIN_OPTIONS:
        return result
    filler = ("是", "不是", "跳过")
    merged: dict[str, None] = dict.fromkeys(result)
    for option in filler:
        merged.setdefault(option, None)
        if len(merged) >= MIN_OPTIONS:
            break
    return tuple(merged)


def build_catalog(registry: IntentRegistry | None = None) -> ClarifierCatalog:
    """从契约构造话术目录（``intents.json`` 优先，``slots.json`` 兜底）。"""
    reg = registry or default_registry()
    per_intent: dict[tuple[str, str], str] = {}
    for spec in reg.all():
        for slot_code, question in spec.clarifier_templates.items():
            text = (question or "").strip()
            if text:
                per_intent[(spec.intent_id, str(slot_code))] = text
    per_slot: dict[str, str] = {}
    for slot in _slot_contracts():
        code = str(slot.get("code", ""))
        question = (slot.get("clarifier_template") or "").strip()
        if code and question:
            per_slot[code] = question
    return ClarifierCatalog(per_intent=per_intent, per_slot=per_slot)


def _slot_contracts() -> tuple[Mapping[str, object], ...]:
    """18 个槽位契约（单一真相源，不复制）。"""
    from app.config import get_contracts  # 局部导入：避免模块级循环

    return tuple(get_contracts().slots)


def default_catalog() -> ClarifierCatalog:
    """进程级默认目录（每次调用重建代价极小，但保持函数式便于测试注入）。"""
    return build_catalog(default_registry())


def pick_slot_to_ask(
    missing: Sequence[str],
    *,
    spec: IntentSpec | None = None,
) -> str | None:
    """在缺失槽位里挑**最该问的那一个**（一次只问 1 个）。

    规则：先按契约 ``required_slots`` 的顺序，再按 :data:`_ASK_ORDER`。
    """
    if not missing:
        return None
    pool = set(missing)
    if spec is not None:
        for code in spec.required_slots:
            if code in pool:
                return code
    for code in _ASK_ORDER:
        if code in pool:
            return code
    return missing[0]


def build_clarify(
    intent_id: str,
    slot_code: str,
    *,
    catalog: ClarifierCatalog | None = None,
    dynamic_options: Sequence[str] | None = None,
) -> ClarifyPrompt:
    """构造一个追问（1 槽位 + 2~3 选项）。

    Args:
        intent_id: 当前意图。
        slot_code: 要追问的槽位。
        catalog: 话术目录（默认即时构建）。
        dynamic_options: 动态候选（如候选商品名）；给定时**覆盖**静态选项。

    Returns:
        :class:`~app.voice.output_schema.ClarifyPrompt`
    """
    cat = catalog or default_catalog()
    options = _clip(dynamic_options) if dynamic_options else cat.options_for(intent_id, slot_code)
    return ClarifyPrompt(
        slot=slot_code,
        question=cat.question_for(intent_id, slot_code),
        options=list(options),
    )
