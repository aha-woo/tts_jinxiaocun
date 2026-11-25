"""新手期保护（按成员前 20 笔语音单必出确认卡）+ A 档直记判定。

设计口径（design-review §9.3）
----------------------------
1. **按人计数**（``Member.novice_voice_count`` / ``novice_first_pass_count``）：
   老板与新店员各自走一遍，互不跳步；
2. **达标标准**：满 20 笔时若一次过率 ≥ 70% 自动毕业；若 < 70% 则自动延长至 30 笔；
3. **毕业后行为**：
   - 新手期内每笔**强制出确认卡**；
   - 毕业且成员开启 ``auto_post_enabled`` 后，满足全部 A 档条件的日常简单单可直记
     （落账 + 5s 撤回条）；其余 B/C 档（新货/缺项/高风险/大额/跨日/模糊）仍出确认卡。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.models.org import Member

__all__ = [
    "NOVICE_EXTEND_STEP",
    "NOVICE_MIN_ACCURACY",
    "NOVICE_TARGET_BASE",
    "can_auto_post_tier_a",
    "novice_status_of",
    "record_voice_confirm",
]

NOVICE_TARGET_BASE = 20
NOVICE_EXTEND_STEP = 10
NOVICE_MIN_ACCURACY = 0.70

_TIER_A_INTENTS: frozenset[str] = frozenset({"purchase.create", "sales.create"})


def _effective_target(count: int, first_pass: int) -> int:
    if count < NOVICE_TARGET_BASE:
        return NOVICE_TARGET_BASE
    rate = (first_pass / count) if count > 0 else 1.0
    if rate < NOVICE_MIN_ACCURACY and count < (NOVICE_TARGET_BASE + NOVICE_EXTEND_STEP):
        return NOVICE_TARGET_BASE + NOVICE_EXTEND_STEP
    if count >= (NOVICE_TARGET_BASE + NOVICE_EXTEND_STEP):
        return NOVICE_TARGET_BASE + NOVICE_EXTEND_STEP
    return NOVICE_TARGET_BASE


def novice_status_of(member: Member | None) -> dict[str, Any]:
    """生成前端展示用的新手期状态字典。"""
    if member is None:
        return {
            "count": 0,
            "first_pass_count": 0,
            "target": NOVICE_TARGET_BASE,
            "first_pass_rate": 1.0,
            "in_novice": True,
            "graduated": False,
            "extended": False,
            "auto_post_enabled": False,
            "just_graduated": False,
        }
    count = int(getattr(member, "novice_voice_count", 0) or 0)
    first_pass = int(getattr(member, "novice_first_pass_count", 0) or 0)
    target = _effective_target(count, first_pass)
    rate = round(first_pass / count, 4) if count > 0 else 1.0
    in_novice = count < target
    return {
        "count": count,
        "first_pass_count": first_pass,
        "target": target,
        "first_pass_rate": rate,
        "in_novice": in_novice,
        "graduated": not in_novice,
        "extended": target > NOVICE_TARGET_BASE,
        "auto_post_enabled": bool(getattr(member, "auto_post_enabled", False)),
        "just_graduated": False,
    }


def record_voice_confirm(
    session: Session,
    member_id: uuid.UUID | None,
    *,
    was_edited: bool,
) -> dict[str, Any]:
    """语音卡片确认入账时递增成员新手计数，并判断是否刚刚毕业。"""
    if member_id is None:
        return novice_status_of(None)
    member = session.get(Member, member_id)
    if member is None:
        return novice_status_of(None)

    was_in_novice = bool(novice_status_of(member)["in_novice"])
    member.novice_voice_count = int(member.novice_voice_count or 0) + 1
    if not was_edited:
        member.novice_first_pass_count = int(member.novice_first_pass_count or 0) + 1
    session.flush()

    status = novice_status_of(member)
    just_graduated = bool(was_in_novice and not status["in_novice"])
    if just_graduated and not member.auto_post_enabled:
        # 毕业时默认给用户开启 A 档直记能力（用户可在弹窗/「我的」页切回「每笔都确认」）
        member.auto_post_enabled = True
        session.flush()
        status = novice_status_of(member)
    status["just_graduated"] = just_graduated
    return status


def can_auto_post_tier_a(
    member: Member | None,
    *,
    parse: Any,
    lines: Sequence[Any],
    conflicts: Sequence[Any],
    gaps: Sequence[Mapping[str, Any]],
    spec: Any,
    today_iso: str,
) -> bool:
    """是否允许跳过确认卡直接入账（A 档极严格门禁，宁可弹卡绝不误直记）。"""
    if member is None or spec is None:
        return False
    status = novice_status_of(member)
    if status["in_novice"] or not bool(member.auto_post_enabled):
        return False
    if str(getattr(spec, "intent_id", "")) not in _TIER_A_INTENTS:
        return False
    if str(getattr(spec, "confirm_policy", "")) != "C1":
        return False
    channel = str(getattr(parse, "channel", "template") or "template")
    threshold = 0.70 if channel == "template" else 0.85
    if float(getattr(parse, "confidence", 0.0) or 0.0) < threshold:
        return False
    slots = getattr(parse, "slots", {}) or {}
    source = getattr(parse, "source", {}) or {}
    required_codes = set(getattr(spec, "required_slots", ()) or ())
    if any(source.get(code) == "pending" for code in required_codes):
        return False
    if str(slots.get("PS") or "已结") != "已结":
        return False
    doc_date = str(slots.get("D") or today_iso)
    if doc_date != today_iso:
        return False
    if conflicts or gaps:
        return False
    if len(lines) != 1:
        return False
    line = lines[0]
    product_id = line.get("product_id") if isinstance(line, Mapping) else getattr(line, "product_id", None)
    pending = line.get("pending") if isinstance(line, Mapping) else getattr(line, "pending", False)
    unit_price = line.get("unit_price_fen") if isinstance(line, Mapping) else getattr(line, "unit_price_fen", None)
    amount_fen = int((line.get("amount_fen") if isinstance(line, Mapping) else getattr(line, "amount_fen", 0)) or 0)
    if not product_id or pending or unit_price is None or amount_fen <= 0:
        return False
    return True
