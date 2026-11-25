"""待处理「要补」缺口计算：source=pending 或值为占位词时，必须报出来。"""

from app.services.ledger import StoredCardPayload, _pending_gaps


def test_gaps_pick_up_pending_source() -> None:
    payload = StoredCardPayload(
        intent_id="sales.goods",
        slots={"P": "可乐", "Q": 15, "U": "件", "UP": "待补", "TA": ""},
        source={"P": "asr", "Q": "asr", "U": "asr", "UP": "pending", "TA": "pending"},
    )
    gaps = _pending_gaps(payload)
    codes = {gap["code"] for gap in gaps}
    assert codes == {"UP", "TA"}
    up = next(gap for gap in gaps if gap["code"] == "UP")
    assert up["label"] == "单价"
    assert up["must"] is False
    assert up["unit"] == "件"


def test_gaps_empty_when_everything_present() -> None:
    payload = StoredCardPayload(
        intent_id="purchase.goods",
        slots={"P": "可乐", "Q": 2, "U": "箱", "UP": 35},
        source={"P": "asr", "Q": "asr", "U": "asr", "UP": "asr"},
    )
    assert _pending_gaps(payload) == []


def test_gaps_mark_must_for_product_and_qty() -> None:
    payload = StoredCardPayload(
        intent_id="purchase.goods",
        slots={"P": "待确认", "Q": None, "U": "箱"},
        source={"P": "pending", "Q": "pending", "U": "asr"},
    )
    gaps = {gap["code"]: gap for gap in _pending_gaps(payload)}
    assert gaps["P"]["must"] is True
    assert gaps["Q"]["must"] is True
    assert "U" not in gaps


def test_purchase_without_price_is_fillable() -> None:
    payload = StoredCardPayload(
        intent_id="purchase.goods",
        slots={"P": "可乐", "Q": 2, "U": "箱"},
        source={"P": "asr", "Q": "asr", "U": "asr"},
    )
    gaps = {gap["code"]: gap for gap in _pending_gaps(payload)}
    assert gaps["UP"]["label"] == "单价"
    assert gaps["UP"]["must"] is False
    assert gaps["UP"]["unit"] == "箱"


def test_sales_without_amount_is_fillable() -> None:
    payload = StoredCardPayload(
        intent_id="sales.goods",
        slots={"P": "可乐", "Q": 15, "U": "件"},
        source={"P": "asr", "Q": "asr", "U": "asr"},
    )
    gaps = {gap["code"]: gap for gap in _pending_gaps(payload)}
    assert gaps["TA"]["label"] == "金额"
    assert gaps["TA"]["must"] is False


def test_credit_intent_asks_supplier_only_when_pending() -> None:
    payload = StoredCardPayload(
        intent_id="purchase.credit",
        slots={"P": "可乐", "Q": 2, "U": "箱", "UP": 35, "S": "待补"},
        source={"P": "asr", "Q": "asr", "U": "asr", "UP": "asr", "S": "pending"},
    )
    gaps = {gap["code"]: gap for gap in _pending_gaps(payload)}
    assert "S" in gaps
    assert gaps["S"]["label"] == "供应商"
