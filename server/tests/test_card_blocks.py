"""确认卡片 **7 区块一个都不能少**（验收① / H10）。

卡片是写操作的唯一入口，所以它既是「确认框」也是「账本预览」。
本文件逐区块验证四件事：

1. **7 个区块键**恰好齐全（``BLOCK_KEYS``），少一个就是流程残缺；
2. ``source`` 角标 4 值（``default`` / ``computed`` / ``alias`` / ``pending``）**一律带上**，
   ``computed`` 必须附 ``calc_trace``，``pending`` **不得静默填值**；
3. 主按钮文案**动态带关键值**（「确认入库 2箱 可乐 ¥70」），不是写死的「确认」；
4. 多商品在**卡片层**展开成多行（契约 18 槽位不扩），大屏与手机端同一份区块。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.intent_registry import default_registry
from app.voice.card_builder import (
    BLOCK_KEYS,
    CardBuildError,
    ConfirmCardBuilder,
    primary_verb_of,
)
from app.voice.impact_preview import ENTITY_INVENTORY, CurrentState, DeltaLine, simulate
from app.voice.source_tags import PENDING_PLACEHOLDER, SOURCE_TAGS, validate_sources
from t04_fixtures import lines_for, parse_of, preview_for, seed  # noqa: F401  pytest 夹具


def _mixed_source_parse():
    """一张覆盖全部 4 个「需要提醒」的 source 的解析结果。

    - ``P`` alias（按个人词表命中）
    - ``D`` default（缺省补全「今天」）
    - ``TA`` computed（2 箱 × ¥35 = ¥70，必须带计算式）
    - ``C`` pending（客户没听清 → **留空待确认**，绝不能猜一个名字出来）
    """
    return parse_of(
        "purchase.create",
        slots={"P": "可乐", "Q": 2, "U": "箱", "TA": 7000, "D": "2026-02-01", "C": None},
        source={"P": "alias", "D": "default", "TA": "computed", "C": "pending"},
        calc_traces=["2 箱 × ¥35.00/箱 = ¥70.00"],
    )


def _build(seed_obj, *, parse=None, lines=None, impact=None):
    """造卡片（区块完整性由 ``build`` 内部 ``assert_complete`` 保证）。"""
    return ConfirmCardBuilder().build(
        parse if parse is not None else _mixed_source_parse(),
        card_id="card-1",
        session_id="sess-1",
        asr_text="进了两箱可乐，一件三十五，李婶先欠着",
        asr_confidence=0.72,
        impact=impact,
        lines=lines if lines is not None else lines_for(seed_obj),
    )


# --------------------------------------------------------------------- 7 区块
def test_card_has_exactly_seven_blocks(seed) -> None:
    """区块键集合 == ``BLOCK_KEYS``，且数量恰好 7（多一个少一个都算残缺）。"""
    card = _build(seed)
    blocks = card.blocks()
    assert tuple(blocks) == BLOCK_KEYS
    assert len(blocks) == 7
    # 每个区块都要有内容结构（不能是空 dict 糊过去）
    assert blocks["original"]["asr_text"] == "进了两箱可乐，一件三十五，李婶先欠着"
    assert blocks["original"]["low_confidence_highlight"] is True  # 0.72 < 0.85 必须高亮
    assert blocks["intent"]["intent_id"] == "purchase.create"
    assert blocks["intent"]["risk_level"] == "R1"
    assert blocks["intent"]["confirm_policy"] == "C1"
    assert blocks["slots"], "③ 槽位区为空"
    assert blocks["calc"]["lines"], "④ 计算式区没有行"
    assert "level" in blocks["risk"] and blocks["risk"]["reason_zh"]
    assert blocks["actions"]["actions"], "⑦ 操作区没有按钮"
    card.assert_complete()  # 幂等自检


def test_big_screen_and_phone_share_the_same_seven_blocks(seed) -> None:
    """同一份区块既给手机端也给电脑版大屏（``resizable``）；窄屏只能堆叠，**不许砍区块**。"""
    payload = _build(seed).to_payload()
    assert set(BLOCK_KEYS).issubset(payload)
    assert len([key for key in payload if key in BLOCK_KEYS]) == 7
    assert payload["meta"]["card_id"] == "card-1"
    assert payload["meta"]["route"] == "card"


# ------------------------------------------------------------------ source 角标
def test_every_source_value_carries_its_tag(seed) -> None:
    """4 个需要提醒的 source 都必须带角标与底色，``asr`` 不打扰用户。"""
    card = _build(seed)
    slots = {slot.code: slot for slot in card.slots}

    assert slots["P"].source == "alias"
    assert slots["P"].tag == SOURCE_TAGS["alias"].label == "按你的习惯"
    assert slots["D"].source == "default"
    assert slots["D"].tag == "缺省补全" and slots["D"].tone == "muted"
    assert slots["TA"].source == "computed"
    assert slots["TA"].tag == "换算得到"
    assert slots["TA"].calc_trace == "2 箱 × ¥35.00/箱 = ¥70.00", "computed 没带计算式"
    assert slots["Q"].source == "asr"
    assert slots["Q"].tag is None, "asr 不该加角标（否则每个槽位都在报警）"


def test_pending_slot_never_shows_a_guessed_value(seed) -> None:
    """``pending`` 必须留空显示「待确认」，**不得静默填值**。"""
    card = _build(seed)
    pending_slot = next(slot for slot in card.slots if slot.code == "C")
    assert pending_slot.source == "pending"
    assert pending_slot.value is None, "pending 槽位被静默填了值"
    assert pending_slot.display == PENDING_PLACEHOLDER == "待确认"
    # 有待确认项时，第一个按钮必须是「先确认黄色项」而不是「确认无误」
    assert card.actions[0].key == "resolve_pending"


def test_computed_without_calc_trace_is_rejected(seed) -> None:
    """``computed`` 却没给计算式 → 卡片构造直接失败（禁止黑箱数字）。"""
    parse = parse_of(
        "purchase.create",
        slots={"P": "可乐", "Q": 2, "TA": 7000},
        source={"TA": "computed"},
        calc_traces=(),
    )
    with pytest.raises(CardBuildError) as excinfo:
        ConfirmCardBuilder().build(
            parse, card_id="c", session_id="s", asr_text="x", asr_confidence=0.9, lines=lines_for(seed)
        )
    assert "calc_trace" in str(excinfo.value) or "计算式" in str(excinfo.value)


def test_validate_sources_rules_are_explicit() -> None:
    """四条 source 规则逐条可执行（不是文档里的一句话）。"""
    assert validate_sources({"P": "asr"}, slots={"P": "可乐"}) == []
    assert any("没有 source" in issue for issue in validate_sources({}, slots={"P": "可乐"}))
    assert any("calc_trace" in issue for issue in validate_sources({"TA": "computed"}, slots={"TA": 1}))
    assert any("静默填值" in issue for issue in validate_sources({"C": "pending"}, slots={"C": "李婶"}))
    assert any("5 值枚举" in issue for issue in validate_sources({"P": "guess"}, slots={"P": "可乐"}))


# --------------------------------------------------------------- 主按钮文案
def test_primary_label_carries_key_values(seed) -> None:
    """主按钮必须写清「做什么、多少、什么货、多少钱」——写死「确认」等于让用户闭眼点。"""
    card = _build(seed)
    label = card.primary_label
    assert label.startswith("确认入库"), label
    assert "可乐" in label, label
    assert "2箱" in label, label
    assert "¥70" in label or "70.00" in label, label
    assert label != "确认", "主按钮文案退化成写死的「确认」"


def test_primary_verb_comes_from_intent_meaning() -> None:
    """动词按意图语义取（入库/卖出/盘点/收款…），缺省回落到契约中文名。"""
    registry = default_registry()
    assert primary_verb_of(registry.require("purchase.create")) == "确认入库"
    assert primary_verb_of(registry.require("sales.create")) == "确认卖出"
    assert primary_verb_of(registry.require("stock.take")) == "确认盘点"
    assert primary_verb_of(registry.require("price.update")) == "确认改价"
    assert primary_verb_of(registry.require("sys.teach")) == "确认记住这个说法"


# -------------------------------------------------------------- 多商品展开成行
def test_multi_product_expands_into_multiple_lines(seed) -> None:
    """``*_multi`` 在**卡片层**展开成多行（契约 18 槽位不扩，不回头改 schema）。"""
    lines = [
        {
            "product_id": str(seed.product_id),
            "product_label": "可乐 500ml",
            "quantity": 2,
            "unit": "箱",
            "qty_base": 48,
            "unit_price_fen": 3500,
            "amount_fen": 7000,
            "payment_status": "已结",
        },
        {
            "product_id": str(seed.product_id),
            "product_label": "红茶 500ml",
            "quantity": 1,
            "unit": "箱",
            "qty_base": 24,
            "unit_price_fen": 4200,
            "amount_fen": 4200,
            "payment_status": "已结",
        },
    ]
    card = ConfirmCardBuilder().build(
        parse_of("purchase.create_multi", slots={"P": "可乐、红茶", "Q": 3}),
        card_id="c-multi",
        session_id="s-multi",
        asr_text="进两箱可乐一箱红茶",
        asr_confidence=0.93,
        lines=lines,
    )
    assert len(card.lines) == 2, "多商品没有展开成多行"
    assert [line.product_label for line in card.lines] == ["可乐 500ml", "红茶 500ml"]
    assert len(card.blocks()["calc"]["lines"]) == 2
    assert "2 个商品" in card.primary_label
    assert "¥112" in card.primary_label or "112.00" in card.primary_label, card.primary_label


# ------------------------------------------------------------------ 预演进卡片
def test_impact_block_shows_before_and_after(seed) -> None:
    """⑤ 影响预演区展示「改前 → 改后」，且带 version（落账 CAS 的唯一依据）。"""
    current = (
        CurrentState(
            entity=ENTITY_INVENTORY,
            key=str(seed.product_id),
            label="可乐 500ml",
            version=7,
            qty_base=Decimal("192"),
            unit="瓶",
        ),
    )
    delta = DeltaLine(
        entity=ENTITY_INVENTORY,
        key=str(seed.product_id),
        label="可乐 500ml",
        qty_delta_base=Decimal("48"),
        unit="瓶",
        calc_trace="2 箱 → 48 瓶",
    )
    bundle = simulate(current, (delta,))
    card = _build(seed, impact=bundle)
    impact = card.blocks()["impact"]
    assert impact[0]["before"] == "192" and impact[0]["after"] == "240"
    assert impact[0]["version"] == 7
    assert card.preview_version == 7
    assert card.to_payload()["meta"]["preview_version"] == 7


def test_risk_banner_escalates_on_preview_warning(seed) -> None:
    """预演有警告时风险条必须变 ``warn``（不能照样显示「常规记账」）。"""
    current = (
        CurrentState(
            entity=ENTITY_INVENTORY,
            key=str(seed.product_id),
            label="可乐 500ml",
            version=0,
            qty_base=Decimal("10"),
            unit="瓶",
        ),
    )
    delta = DeltaLine(
        entity=ENTITY_INVENTORY,
        key=str(seed.product_id),
        label="可乐 500ml",
        qty_delta_base=Decimal("-99"),
        unit="瓶",
        mode="decrease",
    )
    card = _build(seed, impact=simulate(current, (delta,)))
    assert card.risk_banner.level == "warn"
    assert "库存将变负" in card.risk_banner.reason_zh


# ------------------------------------------------------------ 不该出卡片就炸
@pytest.mark.parametrize("intent_id", ["stock.query", "report.export", "partner.merge", "product.unit.set"])
def test_readonly_and_r3_cannot_build_cards(seed, intent_id: str) -> None:
    """R0 / R3 构造卡片 → 直接抛错（不是「构造出来但不让点」）。"""
    with pytest.raises(CardBuildError) as excinfo:
        ConfirmCardBuilder().build(
            parse_of(intent_id, *([{"P": "可乐"}] if intent_id == "stock.query" else [])),
            card_id="c",
            session_id="s",
            asr_text="随便说说",
            asr_confidence=0.9,
            lines=lines_for(seed),
        )
    message = str(excinfo.value)
    assert "R0" in message or "R3" in message, message


def test_missing_required_slot_cannot_build_card(seed) -> None:
    """缺必填槽位 → 不出写卡（宁可让用户补一句，也不要瞎猜一个数）。"""
    with pytest.raises(CardBuildError) as excinfo:
        ConfirmCardBuilder().build(
            parse_of("purchase.create", slots={"P": "可乐"}),  # 缺 Q
            card_id="c",
            session_id="s",
            asr_text="进了点可乐",
            asr_confidence=0.9,
        )
    assert "缺必填槽位" in str(excinfo.value)


def test_confirm_actions_always_offer_button_fallback(seed) -> None:
    """C2 也必须有按钮兜底（禁止「只能语音确认」，更禁止超时自动执行）。"""
    card = ConfirmCardBuilder().build(
        parse_of("stock.take", slots={"P": "可乐", "Q": 8}),
        card_id="c-c2",
        session_id="s-c2",
        asr_text="盘点，可乐还剩八箱",
        asr_confidence=0.95,
        lines=lines_for(seed, quantity=8, qty_base=192),
    )
    assert card.confirm_policy == "C2"
    confirm = next(action for action in card.actions if action.key == "confirm")
    assert confirm.requires_voice is True
    assert confirm.button_fallback is True, "C2 没有按钮兜底"
    assert card.risk_banner.level == "danger"  # R2 必须显眼


def test_unit_conversion_pending_gap_shows_spoken_unit_not_blank(seed) -> None:
    """单位未设换算（U=pending）时，gaps 必须点明哪个单位未设换算，而不是报「单位：没填」。"""
    parse = parse_of(
        "purchase.create",
        slots={"P": "康师傅红烧牛肉面", "Q": 25, "U": "待教学", "UP": 5800, "TA": 145000},
        source={"P": "asr", "Q": "asr", "U": "pending", "UP": "asr", "TA": "computed"},
        calc_traces=["25 × ¥58.00 = ¥1450.00"],
    )
    line = {
        "product_id": str(seed.product_id),
        "product_label": "康师傅红烧牛肉面",
        "quantity": 25,
        "unit": "件",
        "qty_base": None,
        "unit_price_fen": 5800,
        "amount_fen": 145000,
        "payment_status": "已结",
        "pending": True,
    }
    card = ConfirmCardBuilder().build(
        parse,
        card_id="c-u-pending",
        session_id="s-u-pending",
        asr_text="进货，康师傅红烧牛肉面，25件，单价58元",
        asr_confidence=0.95,
        lines=[line],
    )
    payload = card.to_payload()
    unit_gap = next(gap for gap in payload["gaps"] if gap["code"] == "U")
    assert unit_gap["label"] == "单位换算"
    assert "件" in unit_gap["current"]
    assert unit_gap["must"] is True
