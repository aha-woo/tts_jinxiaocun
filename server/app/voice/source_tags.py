"""`source` 角标：**5 值枚举 → 卡片表现**的唯一映射（架构 §2.3 ⑥ H4/H6）。

为什么单独一个模块
------------------
`source` 不是装饰性字段，它决定用户**要不要多看一眼**：

- ``default``：缺省补全（`D=今天` / `PS=已结` / `W=主仓` / `S=待补` / `C=散客`）——
  必须让用户知道「这几个字你没说，是我填的」；
- ``computed``：换算/反算得到，**必须同时给计算式**，否则用户无法验证；
- ``alias``：按个人词表命中（「快乐水」→ 可乐），要让用户知道「我按你的习惯认的」；
- ``pending``：**待确认，不得静默填值**——多候选、缺换算系数、老价无历史时，
  宁可把值留空让用户点，也不要猜一个数字出来（猜错就是账差）；
- ``asr``：直接识别，正常显示。

把这套规则散落在卡片构造、API 序列化、前端模板三处，迟早会出现
「这层显示黄色待确认、那层当正常值用」的分叉，所以集中在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "SOURCE_ORDER",
    "SOURCE_TAGS",
    "PENDING_PLACEHOLDER",
    "SourceTag",
    "display_value",
    "tag_for",
    "validate_sources",
]

#: 契约里的 5 个 source（顺序即展示优先级：越靠前越需要提醒）
SOURCE_ORDER: tuple[str, ...] = ("pending", "computed", "alias", "default", "asr")

#: `pending` 的展示占位：**绝不把猜出来的值直接显示成正常值**
PENDING_PLACEHOLDER = "待确认"


@dataclass(frozen=True)
class SourceTag:
    """一个 source 的展示规则。"""

    source: str
    #: 角标中文（``asr`` 无角标）
    label: str | None
    #: 底色：``normal`` / ``muted`` / ``alias`` / ``warning``
    tone: str
    #: 是否必须附带计算式（``computed`` 强制）
    requires_trace: bool
    #: 是否需要在卡片上「显著提醒」（低置信高亮、风险条）
    needs_attention: bool

    @property
    def shows_tag(self) -> bool:
        """是否显示角标。"""
        return self.label is not None


#: 5 值枚举的展示规则（键与 `source-enum.json` 完全一致，缺一个即启动期报错）
SOURCE_TAGS: Mapping[str, SourceTag] = {
    "asr": SourceTag("asr", None, "normal", False, False),
    "default": SourceTag("default", "缺省补全", "muted", False, True),
    "computed": SourceTag("computed", "换算得到", "muted", True, True),
    "alias": SourceTag("alias", "按你的习惯", "alias", False, False),
    "pending": SourceTag("pending", PENDING_PLACEHOLDER, "warning", False, True),
}


def tag_for(source: str | None) -> SourceTag:
    """取某个 source 的展示规则。

    Args:
        source: ``asr`` / ``default`` / ``computed`` / ``alias`` / ``pending``。

    Returns:
        :class:`SourceTag`；``None`` 按 ``asr`` 处理（规则层未标来源 = 直接识别）。

    Raises:
        KeyError: source 不在 5 值枚举内（防止契约外的新值悄悄流进卡片）。
    """
    key = (source or "asr").strip()
    if key not in SOURCE_TAGS:
        raise KeyError(
            f"未登记的 source {source!r}；契约只允许 {sorted(SOURCE_TAGS)}"
        )
    return SOURCE_TAGS[key]


def display_value(code: str, value: Any, source: str | None) -> str:
    """槽位在卡片上的展示文本（``pending`` 一律显示占位，不显示猜测值）。

    Args:
        code: 槽位代号（``P`` / ``Q`` / …）。
        value: 槽位值（可能是 ``None``）。
        source: 来源。

    Returns:
        可直接渲染的字符串。
    """
    tag = tag_for(source)
    if tag.source == "pending":
        return PENDING_PLACEHOLDER
    if value is None or value == "":
        return "—"
    if code == "Q" and isinstance(value, (int, float)):
        # 数量去掉多余小数尾巴（2.0 → 2），卡片上没人想看 2.0 箱
        return f"{value:g}" if isinstance(value, float) else str(value)
    return str(value)


def validate_sources(
    source_map: Mapping[str, str],
    *,
    slots: Mapping[str, Any] | None = None,
    calc_traces: Iterable[str] = (),
) -> list[str]:
    """校验一张卡片的 source 标注是否合规（返回问题列表，空列表 = 合规）。

    规则（架构 §2.3 ⑥）：
    1. 每个已填槽位都必须有 source（**不许有值没来源**）；
    2. ``computed`` 必须有至少一条计算式；
    3. ``pending`` **不得同时存在值**（否则就是「静默填值」）；
    4. source 必须落在 5 值枚举内。
    """
    problems: list[str] = []
    traces = [trace for trace in calc_traces if str(trace).strip()]
    filled = dict(slots or {})

    for code in filled:
        if code not in source_map:
            problems.append(f"槽位 {code} 有值但没有 source 标注")
    for code, source in source_map.items():
        if source not in SOURCE_TAGS:
            problems.append(f"槽位 {code} 的 source={source!r} 不在 5 值枚举内")
            continue
        if SOURCE_TAGS[source].requires_trace and not traces:
            problems.append(f"槽位 {code} 标为 {source} 但没有任何 calc_trace")
        if source == "pending" and filled.get(code) not in (None, ""):
            problems.append(
                f"槽位 {code} 标为 pending 却已经有值 {filled[code]!r}（静默填值，禁止）"
            )
    return problems
