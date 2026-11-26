"""卡片「要补」缺口：卡片页与待处理列表共用同一套判据，避免两处规则漂移。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: code → (人话标签, 是不是不补就不能确认)
GAP_LABELS: dict[str, tuple[str, bool]] = {
    "P": ("商品", True),
    "Q": ("数量", True),
    "U": ("单位", True),
    "UP": ("单价", False),
    "TA": ("金额", False),
    "C": ("客户", False),
    "S": ("供应商", False),
    "D": ("日期", False),
    "PS": ("付款状态", False),
}

_PLACEHOLDERS = (None, "", "待确认", "待补")


def gaps_for(
    intent_id: str,
    values: Mapping[str, Any],
    sources: Mapping[str, str],
    spoken_unit: str = "",
) -> list[dict[str, Any]]:
    """算出这张卡还缺什么。

    判据与端上约定一致：
    - `sources[code] == "pending"`，或值为空 / 占位词；
    - 客户 / 供应商只在赊账意图里算；
    - 槽位没出现过、也没标 pending 的，不报；
    - 进货缺单价、卖货缺金额，按意图各补一条（可后补）。
    - 当单位 `U` 已有值但未设置换算系数（`source == "pending"`）时，
      明确提示「哪个单位未设换算」，而不是误报成「单位：没填」。
    """
    gaps: list[dict[str, Any]] = []
    unit = str(values.get("U") or "")
    ps_value = str(values.get("PS") or "")
    credit = ("credit" in intent_id) or (ps_value in ("未结", "赊账", "部分"))

    for code, (label, must) in GAP_LABELS.items():
        if code in ("C", "S") and not credit:
            continue
        source = sources.get(code, "")
        if code not in values and source != "pending":
            continue
        value = values.get(code)
        if source != "pending" and value not in _PLACEHOLDERS:
            continue
        if code == "U" and source == "pending":
            hint_unit = (
                spoken_unit.strip()
                if spoken_unit and spoken_unit.strip() not in _PLACEHOLDERS
                else (str(value).strip() if value not in _PLACEHOLDERS else "")
            )
            if hint_unit:
                gaps.append(
                    {
                        "code": "U",
                        "label": "单位换算",
                        "unit": "",
                        "must": must,
                        "current": f"「{hint_unit}」未设换算，请改用商品基本单位",
                    }
                )
                continue
        gaps.append(
            {
                "code": code,
                "label": label,
                "unit": unit if code in ("Q", "UP", "TA") else "",
                "must": must,
                "current": "" if value in _PLACEHOLDERS else str(value),
            }
        )

    known = {gap["code"] for gap in gaps}
    if "purchase" in intent_id and "UP" not in values and "UP" not in known:
        gaps.append({"code": "UP", "label": "单价", "unit": unit, "must": False, "current": ""})
    if "sales" in intent_id and "TA" not in values and "TA" not in known:
        gaps.append({"code": "TA", "label": "金额", "unit": unit, "must": False, "current": ""})
    return gaps
