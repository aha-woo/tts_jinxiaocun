"""卡片编辑的**本地结构化重算**（验收⑥：编辑不重调 AI）。

为什么单独一个纯函数模块
----------------------
编辑是「用户说错了，他自己改一下」——这是一次**表单赋值**，不是一次重新理解。
把它写成「拿着新文本再解析一遍」有两个后果：

1. 花一次模型钱（本来 0 成本的事变成计费）；
2. 可能改出**别的**结果（模型这次把「两箱」读成「两箱半」），用户会更困惑。

所以这里只做**机械重算**：改数量就重算金额，改单价就重算金额，改单位就重算折算量。
不涉及任何自然语言理解，因此可以被穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

if TYPE_CHECKING:  # 仅类型标注：运行时导入会与 ledger 形成循环
    from app.services.ledger import StoredCardPayload

__all__ = ["EditResult", "apply_slot_updates"]

#: 会触发明细重算的槽位
_MONEY_AFFECTING: frozenset[str] = frozenset({"P", "Q", "UP", "TA", "U"})

#: 哪些槽位改了要标 ``asr``（用户亲手给的，不再算缺省/换算）
_USER_SLOTS: frozenset[str] = frozenset({"P", "Q", "U", "UP", "TA", "C", "S", "D", "PS", "N"})


@dataclass
class EditResult:
    """一次编辑的结果。"""

    payload: "StoredCardPayload"
    changes: tuple[tuple[str, Any, Any], ...] = ()
    detail: str = ""

    def original_fragment_of(self, slot_code: str) -> str | None:
        """取该槽位在原话里对应的片段（回流成个人词表时用）。

        这里给的是整句原话里的**槽位值本身**——把「用户改成什么」和「他原来怎么说的」
        一起记下来，回流才有意义；拿不到就返回 ``None``，不编。
        """
        old = next((change[1] for change in self.changes if change[0] == slot_code), None)
        if old in (None, ""):
            return None
        return str(old)


def apply_slot_updates(
    *,
    payload: "StoredCardPayload",
    updates: Mapping[str, Any],
    qty_base_of: Callable[[str | None, Any, str], Decimal] | None = None,
) -> EditResult:
    """把 ``updates`` 应用到卡片载荷（**纯函数**，不碰 DB、不调模型）。

    Args:
        payload: 原卡片载荷。
        updates: ``{槽位代号: 新值}``。
        qty_base_of: 折算函数 ``(product_id, quantity, unit) -> Decimal``；
            不给就按 1:1 折算（缺系数**不臆造**）。

    Returns:
        :class:`EditResult`。
    """
    slots = dict(payload.slots)
    source = dict(payload.source)
    changes: list[tuple[str, Any, Any]] = []

    for slot_code, new_value in updates.items():
        code = str(slot_code).upper()
        old_value = slots.get(code)
        if old_value == new_value:
            continue
        slots[code] = new_value
        # 用户亲手改的值一律标 asr：它既不是缺省补全，也不是换算得到
        source[code] = "asr" if code in _USER_SLOTS else source.get(code, "asr")
        changes.append((code, old_value, new_value))

    lines = [dict(line) for line in payload.lines]
    calc_traces = [str(item) for item in payload.calc_traces if str(item).strip()]
    updated_codes = {change[0] for change in changes}
    if any(code in _MONEY_AFFECTING for code in updated_codes) and lines:
        lines = _recalc_lines(
            lines,
            slots,
            source=source,
            updated_codes=updated_codes,
            qty_base_of=qty_base_of,
            calc_traces=calc_traces,
        )

    from app.services.ledger import StoredCardPayload  # 局部导入：打断循环依赖

    updated = StoredCardPayload(
        intent_id=payload.intent_id,
        slots=slots,
        source=source,
        lines=tuple(lines),
        asr_text=payload.asr_text,
        asr_confidence=payload.asr_confidence,
        calc_traces=tuple(calc_traces),
        expires_at=payload.expires_at,
        edited=payload.edited or bool(changes),
    )
    return EditResult(
        payload=updated,
        changes=tuple(changes),
        detail=f"本地重算 {len(changes)} 个槽位，0 次模型调用",
    )


def _recalc_lines(
    lines: Sequence[Mapping[str, Any]],
    slots: dict[str, Any],
    *,
    source: dict[str, str],
    updated_codes: set[str],
    qty_base_of: Callable[[str | None, Any, str], Decimal] | None,
    calc_traces: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按槽位重算每行的商品/数量/单价/金额（不重调 AI）。"""
    recalculated: list[dict[str, Any]] = []
    single = len(lines) == 1
    ta_explicit = "TA" in updated_codes and slots.get("TA") is not None
    q_or_up_changed = bool({"Q", "UP"} & updated_codes)

    for line in lines:
        row = dict(line)
        if single:
            if "P" in updated_codes and slots.get("P"):
                new_label = str(slots["P"])
                if new_label != str(row.get("product_label") or ""):
                    row["product_label"] = new_label
                    row["product_id"] = None
            row["quantity"] = slots.get("Q", row.get("quantity"))
            row["unit"] = str(slots.get("U") or row.get("unit") or "")
            if slots.get("UP") is not None:
                row["unit_price_fen"] = int(slots["UP"])
        qty = row.get("quantity")
        unit = str(row.get("unit") or "")
        if qty_base_of is not None:
            row["qty_base"] = str(qty_base_of(row.get("product_id"), qty, unit))
        unit_price = row.get("unit_price_fen")
        if ta_explicit and single:
            row["amount_fen"] = int(slots["TA"])
        elif (q_or_up_changed or slots.get("TA") is None) and unit_price is not None and qty is not None:
            row["amount_fen"] = int(
                (Decimal(str(unit_price)) * _num(qty)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        elif slots.get("TA") is not None and single:
            row["amount_fen"] = int(slots["TA"])
        recalculated.append(row)

    if single and recalculated and recalculated[0].get("amount_fen") is not None:
        # 让 slots 与行保持一致（否则卡片 ③ 与 ④ 区会互相打脸）
        ta_fen = int(recalculated[0]["amount_fen"])
        slots["TA"] = ta_fen
        if not ta_explicit and q_or_up_changed:
            source["TA"] = "computed"
            if calc_traces is not None:
                row0 = recalculated[0]
                qty0 = row0.get("quantity")
                unit0 = str(row0.get("unit") or "")
                up0 = row0.get("unit_price_fen")
                calc_traces[:] = [t for t in calc_traces if not t.startswith("总金额")]
                if up0 is not None and qty0 is not None:
                    calc_traces.append(
                        f"总金额 = {qty0}{unit0} × ¥{Decimal(int(up0)) / 100:.2f} = ¥{Decimal(ta_fen) / 100:.2f}"
                    )
                else:
                    calc_traces.append(f"总金额 = ¥{Decimal(ta_fen) / 100:.2f}")
    return recalculated


def _num(value: Any) -> Decimal:
    """安全转 Decimal。"""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
