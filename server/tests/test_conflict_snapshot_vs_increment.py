"""12 类冲突回归集：**盘点快照 vs 增量入库**是第一号红线（PRD §2.6 ①）。

为什么这个文件单独存在
----------------------
「还剩 8 箱」和「再入 8 箱」在字面上只差一个词，但一个是**覆盖式快照**
（把库存设成 8），另一个是**增量入库**（在现有基础上 +8）。
判反一次，库存直接错成两倍 —— 这是全系统最贵的一个 bug，
所以它必须有独立、逐类、可点名到用例的回归集，而不是混在大文件里碰运气。

本文件的断言结构
----------------
1. ``CONFLICT_SPECS`` 必须是**恰好 12 条**且编号 C1~C12（多一类就要改 PRD 与文档）；
2. 每类冲突至少**一个**用例（``CASES`` 表，用例名可追溯到冲突编号）；
3. 第一号冲突有**双向**断言：快照不能被当成增量，增量也不能被当成快照；
4. 单位换算「无系数」时**只能 pending**，代码不许出现「常见值兜底」。
"""

from __future__ import annotations

import pytest

from app.domain.intent_registry import default_registry
from app.voice.alias_resolver import AliasResolver, ProductEntry
from app.voice.conflict_detector import (
    CONFLICT_SPECS,
    ConflictContext,
    blocking_conflicts,
    detect,
    detect_all,
)
from app.voice.template_engine import TemplateEngine
from app.voice.template_rule import SlotSpan
from voice_demo import TODAY, demo_partners_resolver, demo_products, demo_resolver

#: 12 类冲突编号（与 PRD §2.6 一一对应，顺序即文档顺序）
EXPECTED_CODES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12")


def _ctx(**kwargs) -> ConflictContext:
    """构造检测上下文（默认空）。"""
    base: dict = {"text": "", "intent_id": "purchase.create"}
    base.update(kwargs)
    return ConflictContext(**base)


def test_exactly_twelve_conflict_specs() -> None:
    """冲突类别**恰好 12 条**，编号与 PRD 对齐（不允许悄悄加第 13 类）。"""
    assert tuple(spec.code for spec in CONFLICT_SPECS) == EXPECTED_CODES
    assert len(CONFLICT_SPECS) == 12
    for spec in CONFLICT_SPECS:
        assert spec.prd_ref.startswith("PRD §2.6"), spec
        assert spec.name_zh and spec.default_resolution


# --------------------------------------------------------------------------- C1
def test_snapshot_phrase_is_not_treated_as_increment() -> None:
    """「还剩 8 箱」是**快照设定**：不能记成「又进了 8 箱」。"""
    engine = TemplateEngine()
    outcome = engine.run("盘点，仓库可乐还剩 8 箱", today_iso=TODAY, resolver=demo_resolver())
    assert outcome.hit is True
    assert outcome.intent_id == "stock.take", "「还剩 N」必须归盘点快照"
    assert outcome.result is not None
    assert outcome.result.slots["Q"] == 8
    # 快照语义：路线是出卡（覆盖式），且没有增量冲突
    assert not any(conflict.code == "C1" for conflict in outcome.conflicts)


def test_increment_phrase_is_not_treated_as_snapshot() -> None:
    """「再入 8 箱」是**增量**：不能把库存覆盖成 8。"""
    engine = TemplateEngine()
    outcome = engine.run("再入 8 箱可乐", today_iso=TODAY, resolver=demo_resolver())
    assert outcome.hit is True
    assert outcome.intent_id.startswith("purchase."), "「再入 N」必须归增量入库"
    assert not any(conflict.code == "C1" for conflict in outcome.conflicts)


def test_mixed_phrases_are_blocked_with_clarify() -> None:
    """同一句里既有「还剩」又有「再入」→ **阻断**并追问，不许二选一猜。"""
    engine = TemplateEngine()
    outcome = engine.run("盘点，再入 8 箱可乐", today_iso=TODAY, resolver=demo_resolver())
    assert outcome.hit is True
    assert outcome.blocked is True
    blocking = blocking_conflicts(outcome.conflicts)
    assert [conflict.code for conflict in blocking] == ["C1"]
    assert blocking[0].clarify_slot is not None
    assert outcome.result is not None and outcome.result.route == "clarify"


# --------------------------------------------------------------------------- 12 类逐条
#: ``(冲突编号, 用例名, 构造上下文的 kwargs)`` —— 回归表的**唯一登记处**
CASES: tuple[tuple[str, str, dict], ...] = (
    (
        "C1",
        "snapshot_vs_increment",
        {"text": "盘点，再入 8 箱可乐", "intent_id": "stock.take"},
    ),
    (
        "C2",
        "sale_vs_return",
        {"text": "客户退了两瓶，钱已经退给人家了", "intent_id": "sales.create"},
    ),
    (
        "C3",
        "unit_ratio_missing",
        {"text": "矿泉水三箱", "intent_id": "stock.take", "slots": {"U": "箱"}, "source": {"U": "pending"}},
    ),
    (
        "C4",
        "amount_vs_unit_price",
        {"text": "可乐 35", "intent_id": "sales.create", "amount_rule": "bare_number_ambiguous"},
    ),
    (
        "C5",
        "product_alias_conflict",
        {"text": "可乐两箱", "intent_id": "purchase.create", "product_ambiguous": True,
         "product_options": ("可乐 500ml", "可乐 2L", "百事可乐 500ml")},
    ),
    (
        "C6",
        "fuzzy_date",
        {"text": "上次那张单的钱", "intent_id": "doc.query", "date_needs_confirm": True},
    ),
    (
        "C7",
        "negative_or_reversal",
        {"text": "记一下，-2 箱可乐", "intent_id": "purchase.create", "slots": {"Q": -2}},
    ),
    (
        "C8",
        "number_reading",
        {"text": "三五箱可乐", "intent_id": "purchase.create"},
    ),
    (
        "C9",
        "payment_status_conflict",
        {"text": "先欠着，不过钱已经付了", "intent_id": "sales.create"},
    ),
    (
        "C10",
        "stale_price",
        {"text": "按老价来两箱可乐", "intent_id": "purchase.create"},
    ),
    (
        "C11",
        "homophone",
        {"text": "这条帐记一下", "intent_id": "sales.create", "corrections": 1},
    ),
    (
        "C12",
        "store_attribution",
        {"text": "给仓库补两箱可乐", "intent_id": "purchase.create",
         "store_names": ("主仓", "二号店")},
    ),
)


@pytest.mark.parametrize(("code", "case_name", "kwargs"), CASES, ids=[case[1] for case in CASES])
def test_each_conflict_class_is_detected(code: str, case_name: str, kwargs: dict) -> None:
    """每一类冲突都必须被**它自己的检测器**抓到（逐类可点名）。"""
    if code == "C8":
        kwargs = dict(kwargs, ambiguous_spans=(
            SlotSpan(code="Q", text="三五", start=0, end=2, value=None,
                     alternatives=("35", "3.5", "3、5")),
        ))
    found = detect_all(_ctx(**kwargs))
    assert found[code] is not None, f"{code}({case_name}) 未被检出"
    conflict = found[code]
    assert conflict.code == code
    assert conflict.name_zh == dict(
        (spec.code, spec.name_zh) for spec in CONFLICT_SPECS
    )[code]


def test_conflict_coverage_table_covers_all_twelve() -> None:
    """覆盖表必须**恰好**覆盖 12 类，且每类至少一个用例（防止漏测）。"""
    covered = {code for code, _name, _kwargs in CASES}
    assert covered == set(EXPECTED_CODES)
    assert len(CASES) == 12


def test_blocking_flags_are_set_for_money_and_stock_risks() -> None:
    """C1/C2/C4/C5/C7/C8 都是**阻断级**：这些错了会直接记错账。"""
    blocking_codes = {"C1", "C2", "C4", "C5", "C7", "C8"}
    for code, _name, kwargs in CASES:
        if code == "C8":
            kwargs = dict(kwargs, ambiguous_spans=(
                SlotSpan(code="Q", text="三五", start=0, end=2, value=None,
                         alternatives=("35", "3.5", "3、5")),
            ))
        conflicts = detect(_ctx(**kwargs))
        assert code in {conflict.code for conflict in conflicts}
        if code in blocking_codes:
            assert any(
                conflict.code == code and conflict.blocked for conflict in conflicts
            ), f"{code} 应为阻断级"


# --------------------------------------------------------------------------- C3 专项
def test_missing_unit_ratio_marks_pending_and_never_invents() -> None:
    """商品没有「箱→瓶」系数时：**只能 pending**，绝不许出现「常见 24 瓶」这种兜底。"""
    products = demo_products()
    mineral = next(entry for entry in products if entry.name == "矿泉水")
    assert mineral.ratios == {}, "夹具里矿泉水故意不带换算系数"

    engine = TemplateEngine()
    outcome = engine.run(
        "盘点，矿泉水还剩三箱",
        today_iso=TODAY,
        resolver=AliasResolver([mineral]),
    )
    assert outcome.hit is True
    assert outcome.result is not None
    assert outcome.result.source["U"] == "pending"
    assert any("待教学" in line for line in outcome.result.calc_traces)
    conflicts = {conflict.code for conflict in outcome.conflicts}
    assert "C3" in conflicts


def test_present_unit_ratio_is_used_and_shown() -> None:
    """有系数时必须给出「2 箱 = 48 瓶」双显，而不是只留原话。"""
    engine = TemplateEngine()
    outcome = engine.run("盘点，可乐还剩两箱", today_iso=TODAY, resolver=demo_resolver())
    assert outcome.result is not None
    assert outcome.result.source["U"] != "pending"
    joined = " ".join(outcome.result.calc_traces)
    assert "48" in joined, f"应出现折算后的 48 瓶，实际：{joined}"
