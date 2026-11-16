"""契约同步校验（T01 验收 ③）：38 intent / 18 slot / 5 source / 错误码齐全且自洽。

这里刻意**不复用** ``app.config.load_contracts`` 的校验逻辑：契约测试要独立断言
原始 JSON，避免「实现和测试一起错」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "packages" / "shared" / "contracts"
SCHEMAS = REPO_ROOT / "packages" / "shared" / "schemas"

EXPECTED_INTENT_IDS = [
    "purchase.create",
    "purchase.create_multi",
    "sales.create",
    "sales.create_multi",
    "sales_return.create",
    "purchase_return.create",
    "stock.take",
    "stock.take_multi",
    "stock.loss",
    "price.update",
    "stock.query",
    "stock.query_movement",
    "stock.query_low",
    "doc.query",
    "doc.query_summary",
    "partner.create",
    "partner.update",
    "partner.merge",
    "debt.query",
    "payment.receive",
    "payment.pay",
    "debt.writeoff",
    "product.create",
    "product.update",
    "product.alias.add",
    "product.unit.set",
    "product.disable",
    "report.today",
    "report.period",
    "report.top",
    "report.export",
    "sys.undo",
    "sys.cancel",
    "sys.continue",
    "sys.correct",
    "sys.help",
    "sys.example",
    "sys.teach",
]

#: PRD §2.8「规则层优先命中」的 18 个 intent（命中即不调 LLM）
EXPECTED_TEMPLATE_HIT = {
    "purchase.create",
    "purchase.create_multi",
    "sales.create",
    "sales.create_multi",
    "stock.take",
    "stock.take_multi",
    "stock.loss",
    "stock.query",
    "doc.query",
    "doc.query_summary",
    "debt.query",
    "product.create",
    "product.alias.add",
    "report.today",
    "sys.cancel",
    "sys.continue",
    "sys.help",
    "sys.example",
}

EXPECTED_SLOT_CODES = [
    "P",
    "Q",
    "U",
    "UP",
    "TA",
    "C",
    "S",
    "D",
    "N",
    "PS",
    "W",
    "AMT",
    "DOC",
    "TF",
    "TV",
    "EF",
    "ALIAS",
    "RATIO",
]

EXPECTED_SOURCES = {"asr", "default", "computed", "alias", "pending"}

V11_ERROR_CODES = {
    "E_PAY_SIGN_INVALID",
    "E_DELIVER_DUPLICATE",
    "E_ENTITLEMENT_EXPIRED",
    "E_AUDIO_TOO_LARGE",
    "E_R3_PC_REQUIRED",
}

RISK_LEVELS = {"R0", "R1", "R2", "R3"}
CONFIRM_POLICIES = {"C0", "C1", "C2", "C3"}
REVERSAL_METHODS = {"reversal_doc", "pending_task", "none"}
INVENTORY_DELTAS = {"increase", "decrease", "snapshot", "adjust", "none"}
AR_AP_DELTAS = {
    "none",
    "receivable_increase",
    "receivable_decrease",
    "payable_increase",
    "payable_decrease",
}


def load_json(path: Path) -> Any:
    """读取 JSON 契约/用例文件。"""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def intents_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "intents.json")


@pytest.fixture(scope="module")
def slots_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "slots.json")


@pytest.fixture(scope="module")
def sources_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "source-enum.json")


@pytest.fixture(scope="module")
def errors_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "error-codes.json")


@pytest.fixture(scope="module")
def domains_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "domains.json")


@pytest.fixture(scope="module")
def goods_doc() -> dict[str, Any]:
    return load_json(CONTRACTS / "goods.json")


def test_intent_count_and_ids(intents_doc: dict[str, Any]) -> None:
    """38 个 intent，id 与 PRD §2.8 清单完全一致且唯一。"""
    intents = intents_doc["intents"]
    assert len(intents) == 38
    ids = [item["intent_id"] for item in intents]
    assert ids == EXPECTED_INTENT_IDS
    assert len(set(ids)) == 38
    assert [item["seq"] for item in intents] == list(range(1, 39))


def test_intent_required_fields(intents_doc: dict[str, Any]) -> None:
    """每个 intent 的必备字段齐全且类型正确。"""
    for intent in intents_doc["intents"]:
        intent_id = intent["intent_id"]
        assert intent["domain"], intent_id
        assert intent["risk_level"] in RISK_LEVELS, intent_id
        assert intent["confirm_policy"] in CONFIRM_POLICIES, intent_id
        assert isinstance(intent["required_slots"], list), intent_id
        assert isinstance(intent["template_hit"], bool), intent_id
        assert isinstance(intent["whitelist_blocked"], bool), intent_id
        assert isinstance(intent["sample_utterances"], list) and intent["sample_utterances"], intent_id
        assert all(isinstance(text, str) and text.strip() for text in intent["sample_utterances"]), intent_id
        assert isinstance(intent["side_effect"], dict), intent_id
        assert isinstance(intent["reversal"], dict), intent_id
        assert intent["milestone"] in {"MVP", "P0", "P1", "P2"}, intent_id


def test_risk_confirm_combinations(intents_doc: dict[str, Any]) -> None:
    """R3 ⇒ C3 且白名单；R0 ⇒ C0 且不写库；非 R3 不得标白名单（附录 A3）。"""
    for intent in intents_doc["intents"]:
        intent_id = intent["intent_id"]
        risk = intent["risk_level"]
        confirm = intent["confirm_policy"]
        if risk == "R3":
            assert confirm == "C3", intent_id
            assert intent["whitelist_blocked"] is True, intent_id
            assert intent["side_effect"]["writes_db"] is False, intent_id
            assert intent["reversal"]["method"] == "pending_task", intent_id
        if risk == "R0":
            assert confirm == "C0", intent_id
            assert intent["side_effect"]["writes_db"] is False, intent_id
            assert intent["reversal"]["allowed"] is False, intent_id
        if risk != "R3":
            assert intent["whitelist_blocked"] is False, intent_id


def test_side_effect_and_reversal_enums(intents_doc: dict[str, Any]) -> None:
    """side_effect / reversal 的枚举值必须在契约声明的集合内。"""
    enums = intents_doc["enums"]
    assert set(enums["risk_level"]) == RISK_LEVELS
    assert set(enums["confirm_policy"]) == CONFIRM_POLICIES
    assert set(enums["reversal_method"]) == REVERSAL_METHODS
    assert set(enums["inventory_delta"]) == INVENTORY_DELTAS
    assert set(enums["ar_ap_delta"]) == AR_AP_DELTAS

    for intent in intents_doc["intents"]:
        intent_id = intent["intent_id"]
        side_effect = intent["side_effect"]
        assert side_effect["inventory_delta"] in INVENTORY_DELTAS, intent_id
        assert side_effect["ar_ap_delta"] in AR_AP_DELTAS, intent_id
        assert isinstance(side_effect["entities"], list), intent_id
        reversal = intent["reversal"]
        assert reversal["method"] in REVERSAL_METHODS, intent_id
        assert isinstance(reversal["allowed"], bool), intent_id
        window = reversal["window_minutes"]
        assert window is None or (isinstance(window, int) and window > 0), intent_id


def test_template_hit_set(intents_doc: dict[str, Any], slots_doc: dict[str, Any]) -> None:
    """18 个 template_hit intent 与 PRD §2.8 规则层清单一致（T03 据此建模板）。"""
    actual = {intent["intent_id"] for intent in intents_doc["intents"] if intent["template_hit"]}
    assert actual == EXPECTED_TEMPLATE_HIT
    assert len(actual) == 18
    assert slots_doc["count"] == 18


def test_slot_codes(intents_doc: dict[str, Any], slots_doc: dict[str, Any]) -> None:
    """18 个槽位代号完整、唯一，且 intent 引用的槽位都已登记。"""
    slots = slots_doc["slots"]
    assert len(slots) == 18
    codes = [slot["code"] for slot in slots]
    assert codes == EXPECTED_SLOT_CODES
    assert set(codes) == set(EXPECTED_SLOT_CODES)

    for slot in slots:
        for key in ("name", "name_zh", "type", "required_by_default", "default_rule", "display_rule", "validation"):
            assert key in slot, slot["code"]
        assert isinstance(slot["required_by_default"], bool)

    for intent in intents_doc["intents"]:
        referenced = set(intent["required_slots"]) | set(intent["optional_slots"])
        unknown = referenced - set(codes)
        assert not unknown, f"{intent['intent_id']} 引用了未登记槽位 {unknown}"


def test_ctx_internal_marker_documented(slots_doc: dict[str, Any]) -> None:
    """CTX 不是槽位而是内部标记：必须在 internal_markers 中留档（文档「18 vs 19」收口）。"""
    markers = slots_doc["internal_markers"]
    assert [marker["code"] for marker in markers] == ["CTX"]
    assert markers[0]["is_slot"] is False
    assert markers[0]["type"] == "internal_flag"
    # 槽位里不得再出现 CTX
    assert "CTX" not in [slot["code"] for slot in slots_doc["slots"]]


def test_required_slots_not_optional_conflict(intents_doc: dict[str, Any]) -> None:
    """必填槽位不得同时出现在可选槽位里。"""
    for intent in intents_doc["intents"]:
        overlap = set(intent["required_slots"]) & set(intent["optional_slots"])
        assert not overlap, f"{intent['intent_id']} 必填/可选冲突 {overlap}"


def test_domains_referential_integrity(intents_doc: dict[str, Any], domains_doc: dict[str, Any]) -> None:
    """12 域与 intent 双向引用一致。"""
    domains = domains_doc["domains"]
    assert len(domains) == 12
    assert domains_doc["count"] == 12

    domain_codes = {domain["code"] for domain in domains}
    assert len(domain_codes) == 12

    listed: set[str] = set()
    for domain in domains:
        for intent_id in domain["intents"]:
            assert intent_id not in listed, f"{intent_id} 在多个域中重复出现"
            listed.add(intent_id)
    assert listed == set(EXPECTED_INTENT_IDS)

    for intent in intents_doc["intents"]:
        assert intent["domain"] in domain_codes, intent["intent_id"]
        owner = next(domain for domain in domains if domain["code"] == intent["domain"])
        assert intent["intent_id"] in owner["intents"], intent["intent_id"]


def test_source_enum_exactly_five(sources_doc: dict[str, Any]) -> None:
    """source 枚举严格 5 值（asr / default / computed / alias / pending）。"""
    codes = [source["code"] for source in sources_doc["sources"]]
    assert len(codes) == 5
    assert set(codes) == EXPECTED_SOURCES
    assert sources_doc["count"] == 5
    assert sources_doc["closed"] is True
    for source in sources_doc["sources"]:
        assert isinstance(source["badge_required"], bool)
        if source["code"] != "asr":
            assert source["badge_required"] is True, source["code"]


def test_error_codes_complete(errors_doc: dict[str, Any]) -> None:
    """错误码齐全（>=20）、含 v1.1 新增 5 个、numeric 唯一且与 count 一致。"""
    codes = errors_doc["codes"]
    assert len(codes) >= 20
    assert errors_doc["count"] == len(codes)
    assert V11_ERROR_CODES.issubset(codes.keys())

    numerics = [spec["numeric"] for spec in codes.values()]
    assert len(set(numerics)) == len(numerics)
    assert 0 not in numerics
    assert errors_doc["success_code"] == 0

    for code, spec in codes.items():
        assert 400 <= spec["http_status"] <= 599 or spec["http_status"] == 200, code
        assert isinstance(spec["message_zh"], str) and spec["message_zh"], code
        assert isinstance(spec["deduct_quota"], bool), code
        assert spec["category"] in {
            "gate_filter",
            "parse_llm",
            "card_confirm",
            "auth_tenant",
            "payment_entitlement",
            "system",
        }, code

    # R3 相关两个错误码必须引导「电脑版微信大屏管理页」，不得引导网页端
    assert "电脑版微信大屏" in codes["E_R3_PC_REQUIRED"]["message_zh"]
    assert "电脑版微信大屏" in codes["E_R3_BLOCKED"]["message_zh"]
    assert "comment" in codes["E_R3_PC_REQUIRED"]


def test_goods_contract(goods_doc: dict[str, Any]) -> None:
    """道具契约：无自动续费、最低 1 元、价格单位为分。"""
    assert goods_doc["constraints"]["auto_renew_always_false"] is True
    assert goods_doc["constraints"]["min_payment_fen"] == 100
    assert goods_doc["constraints"]["payment_channel"] == "wx.requestVirtualPayment"
    assert set(goods_doc["plan_codes"]) == {"free", "30day", "quota_pack"}

    for goods in goods_doc["goods"]:
        assert goods["goods_type"] in {"entitlement", "quota_pack"}, goods["goods_id"]
        assert isinstance(goods["price_fen"], int) and goods["price_fen"] > 0, goods["goods_id"]
        assert goods["auto_renew"] is False, goods["goods_id"]
        assert goods["status"] in {"on_shelf", "off_shelf"}, goods["goods_id"]
    assert goods_doc["free_plan"]["auto_renew"] is False


def test_schemas_intent_enum_matches_contract() -> None:
    """parse-result.schema.json 的 intent 枚举必须与 intents.json 完全一致。"""
    schema = load_json(SCHEMAS / "parse-result.schema.json")
    enum = schema["properties"]["intent"]["enum"]
    assert enum == EXPECTED_INTENT_IDS
    assert schema["properties"]["source"]["additionalProperties"]["enum"] == [
        "asr",
        "default",
        "computed",
        "alias",
        "pending",
    ]
    assert "calc_traces" in schema["properties"]
    assert schema["required"] == ["intent", "slots", "source", "confidence", "clarify"]

    voice_schema = load_json(SCHEMAS / "voice-session.schema.json")
    status_enum = voice_schema["properties"]["status"]["enum"]
    assert "AWAIT_CONFIRM" in status_enum and "PENDING_TASK" in status_enum and "CLOSED_READ" in status_enum
    assert len(status_enum) == 16

    entitlement_schema = load_json(SCHEMAS / "entitlement.schema.json")
    assert entitlement_schema["properties"]["auto_renew"]["const"] is False
    assert {"VirtualOrder", "DeliverEvent"} <= entitlement_schema["$defs"].keys()

    card_schema = load_json(SCHEMAS / "confirm-card.schema.json")
    assert len(card_schema["required"]) == 10
    assert "impact_preview" in card_schema["properties"]
