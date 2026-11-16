"""方案 d —— 交易**方向**判据的单元测试（0 LLM，纯确定性）。

整改的根因（team-lead 复核时指出的）
-----------------------------------
规则表把「要 / 要了 / 拿了 / 到了」这四个**方向中性**的词硬塞进了某一侧：
「客户要十件」是销售，「我要进货十箱」是进货；「客户拿走」是销售，「我去拿货」是进货。
方向由**主语与场景**决定，不由这几个字决定。把多义词当方向指示词，必然记错账。

因此本文件把四件事故钉住：

1. 中性词**不参与**方向判定，但在两张关键词表里必须**对称存在**
   （否则「要十件」这类话会直接漏到 G4 花钱）；
2. 方向判据的优先级：明确方向词 > ``partner.type`` > 收付信号 > 都没有则**强制澄清**；
3. 判据互相矛盾（「进货」+ 客户名）→ **不判**，走澄清并说明矛盾；
4. 澄清答完之后意图真的换到对侧，而且**不写任何槽位**（方向不是 18 槽位之一）。
"""

from __future__ import annotations

import pytest

from app.voice.direction import (
    DIRECTION_SLOT,
    NEUTRAL_TRADE_WORDS,
    PURCHASE,
    PURCHASE_DIRECTION_WORDS,
    SALES,
    SALES_DIRECTION_WORDS,
    build_direction_clarify,
    direction_of_intent,
    is_trade_intent,
    resolve_direction,
    switch_trade_intent,
)
from app.voice.template_rule import _PURCHASE_KEYWORDS, _SALES_KEYWORDS
from voice_demo import (
    TODAY,
    build_pipeline,
    demo_customers_resolver,
    demo_resolver,
    demo_suppliers_resolver,
)


# ---------------------------------------------------------------- 词表结构不变量
def test_neutral_words_are_kept_but_never_used_as_direction_evidence() -> None:
    """中性词**只**参与意图识别，绝不进方向词表 —— 这是本轮整改的核心不变量。"""
    for word in NEUTRAL_TRADE_WORDS:
        assert word not in PURCHASE_DIRECTION_WORDS, f"「{word}」是方向中性词，不能算进货方向"
        assert word not in SALES_DIRECTION_WORDS, f"「{word}」是方向中性词，不能算销售方向"


def test_neutral_words_appear_symmetrically_in_both_keyword_tables() -> None:
    """两侧关键词表在中性词上必须**完全对称**，否则「要十件」会漏到 G4。"""
    for word in NEUTRAL_TRADE_WORDS:
        assert word in _PURCHASE_KEYWORDS, f"采购侧缺中性词「{word}」"
        assert word in _SALES_KEYWORDS, f"销售侧缺中性词「{word}」"


def test_direction_word_tables_are_disjoint() -> None:
    """方向词表本身不能有交集（有交集就等于又造了一个多义词）。"""
    overlap = set(PURCHASE_DIRECTION_WORDS) & set(SALES_DIRECTION_WORDS)
    assert overlap == set(), f"方向词表出现共词：{sorted(overlap)}"


# ---------------------------------------------------------------- 判据一：强方向词
@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("今天进了两箱可乐", PURCHASE),
        ("采购了两件旺仔", PURCHASE),
        ("再入了两箱和其正", PURCHASE),
        ("卖了四箱娃哈哈", SALES),
        ("零售出去六瓶红牛", SALES),
        ("出库三瓶可乐", SALES),
    ],
)
def test_strong_direction_word_decides(text: str, expect: str) -> None:
    """方向唯一的词直接定方向，不需要任何其他判据。"""
    verdict = resolve_direction(text)
    assert verdict.direction == expect
    assert verdict.reason == "strong_word"


def test_both_direction_words_in_one_sentence_is_a_conflict() -> None:
    """「进了两箱，卖了一箱」既像进货又像卖货 → 不判、标记矛盾。"""
    verdict = resolve_direction("进了两箱可乐，卖了一箱红牛")
    assert verdict.direction is None
    assert verdict.conflict is True
    assert verdict.reason == "strong_word_conflict"


# ---------------------------------------------------------------- 判据二：partner.type
def test_partner_type_decides_when_no_direction_word() -> None:
    """没有方向词时，命中客户表 → 卖货；命中供应商表 → 进货。"""
    customer = resolve_direction("李婶拿了两箱矿泉水", has_customer=True, customer_names=("李婶",))
    assert (customer.direction, customer.reason) == (SALES, "partner_type")

    supplier = resolve_direction("老张拿了两箱红牛", has_supplier=True, supplier_names=("老张",))
    assert (supplier.direction, supplier.reason) == (PURCHASE, "partner_type")


def test_partner_that_is_both_customer_and_supplier_is_not_judged() -> None:
    """同一家既是客户又是供应商 → 不判（宁可问一句）。"""
    verdict = resolve_direction(
        "拿了两箱可乐",
        has_customer=True,
        has_supplier=True,
        customer_names=("老张",),
        supplier_names=("老张",),
    )
    assert verdict.direction is None
    assert verdict.reason == "partner_type_ambiguous"


# ---------------------------------------------------------------- 判据三：收付信号
@pytest.mark.parametrize(
    ("text", "expect", "reason"),
    [
        ("李婶拿了两箱矿泉水，给了一百", SALES, "money_signal"),
        ("一件矿泉水，现结", SALES, "money_signal"),
        ("结给老王三百块", PURCHASE, "money_signal"),
        ("付了货款两百", PURCHASE, "money_signal"),
    ],
)
def test_money_signal_decides(text: str, expect: str, reason: str) -> None:
    """收钱 → 卖货；付钱给对方 → 进货。"""
    verdict = resolve_direction(text)
    assert verdict.direction == expect
    assert verdict.reason == reason


def test_debt_alone_is_neutral_and_never_decides_direction() -> None:
    """「欠」本身是中性的：客户欠我 = 销售，我欠供应商 = 进货。单靠「欠」不判。"""
    verdict = resolve_direction("拿了两箱可乐，钱先欠着")
    assert verdict.direction is None
    assert verdict.conflict is False


# ---------------------------------------------------------------- 矛盾与缺失
def test_direction_word_and_partner_type_conflict_triggers_clarify() -> None:
    """「进货」+ 客户名 → 判据矛盾，**不判**，并且要告诉用户「听出矛盾了」。"""
    verdict = resolve_direction("进货，李婶拿了两箱", has_customer=True, customer_names=("李婶",))
    assert verdict.direction is None
    assert verdict.conflict is True
    assert any("strong_word" in item for item in verdict.evidence)


def test_no_criterion_at_all_means_ask_the_user() -> None:
    """三类判据都没有 → 不猜，返回空方向（调用方必须走强制澄清）。"""
    verdict = resolve_direction("矿泉水要十件，就是那个五百五十毫升的")
    assert verdict.direction is None
    assert verdict.conflict is False
    assert verdict.reason == "none"


def test_direction_clarify_prompt_is_static_and_has_two_buttons() -> None:
    """方向追问是**静态话术 + 2 个按钮**，0 次 LLM；``slot`` 是伪槽位 DIR。"""
    prompt = build_direction_clarify()
    assert prompt.slot == DIRECTION_SLOT == "DIR"
    assert prompt.options == ["进货", "卖货"]
    assert "进货" in prompt.question and "卖货" in prompt.question
    assert build_direction_clarify(conflict=True).question != prompt.question


# ---------------------------------------------------------------- 意图镜像
@pytest.mark.parametrize(
    ("intent", "direction", "expect"),
    [
        ("purchase.create", SALES, "sales.create"),
        ("sales.create", PURCHASE, "purchase.create"),
        ("purchase.create_multi", SALES, "sales.create_multi"),
        ("sales.create_multi", PURCHASE, "purchase.create_multi"),
        # 已经一致时幂等：不许把对侧又翻回来（这条曾经真的写错过一次）
        ("purchase.create", PURCHASE, "purchase.create"),
        ("sales.create_multi", SALES, "sales.create_multi"),
    ],
)
def test_switch_trade_intent_keeps_multi_suffix_and_is_idempotent(
    intent: str, direction: str, expect: str
) -> None:
    """换方向保留 ``_multi`` 后缀；方向已一致时返回原意图。"""
    assert switch_trade_intent(intent, direction) == expect


def test_non_trade_intents_have_no_direction() -> None:
    """盘点 / 查询 / 退货类不参与方向判定（退货方向由意图自己表达）。"""
    for intent in ("stock.take", "stock.query", "debt.query", "purchase_return.create"):
        assert is_trade_intent(intent) is False
        assert direction_of_intent(intent) is None
        assert switch_trade_intent(intent, SALES) is None


# ---------------------------------------------------------------- 端到端：强制澄清真能救回来
def _run(text: str, session_key: str):
    """跑一次流水线（类型化词表，与生产一致）。"""
    pipeline, counter = build_pipeline()
    result = pipeline.run(
        text,
        today_iso=TODAY,
        resolver=demo_resolver(),
        customers=demo_customers_resolver(),
        suppliers=demo_suppliers_resolver(),
        session_key=session_key,
    )
    return pipeline, counter, result


def test_ambiguous_direction_never_reaches_a_card() -> None:
    """方向判不出来时**不出卡**：route=clarify、问的就是 DIR、而且一次模型都没调。"""
    _pipeline, counter, result = _run("矿泉水要十件", "dir-ambiguous")
    assert result.parse.route == "clarify"
    assert result.parse.clarify is not None
    assert result.parse.clarify.slot == "DIR"
    assert result.parse.clarify.options == ["进货", "卖货"]
    assert counter.calls == 0


def test_customer_with_neutral_verb_is_switched_to_sales() -> None:
    """「李婶拿了两箱…」：中性词「拿了」不决定方向，客户类型把它纠正成卖货。"""
    _pipeline, _counter, result = _run("李婶拿了两箱可乐", "dir-customer")
    assert result.parse.intent == "sales.create"
    assert any("方向=卖货" in line for line in (result.template.trace or ()))


def test_answering_direction_clarify_switches_intent_without_writing_slots() -> None:
    """点「进货」之后：意图换到 purchase.create，**没有任何槽位被这个回答污染**。"""
    pipeline, _counter, result = _run("矿泉水要十件", "dir-answer")
    state = pipeline._clarify.begin(result.parse)  # noqa: SLF001  （测试直接驱动状态机）
    assert state.pending_slot == "DIR"

    answered = pipeline.answer_clarify(state, "进货", base=result.parse)
    parse = answered.parse
    assert parse.intent == "purchase.create"
    assert "DIR" not in parse.slots, "方向不是 18 槽位之一，绝不能写进 slots"
    assert "DIR" not in parse.source
    assert answered.clarify_state is not None
    assert answered.clarify_state.direction == PURCHASE


def test_answering_direction_clarify_can_also_say_sales() -> None:
    """点「卖货」→ sales.create（两侧都能走通）。"""
    pipeline, _counter, result = _run("矿泉水要十件", "dir-answer-sales")
    state = pipeline._clarify.begin(result.parse)  # noqa: SLF001
    answered = pipeline.answer_clarify(state, "卖货", base=result.parse)
    assert answered.parse.intent == "sales.create"
    assert answered.clarify_state is not None
    assert answered.clarify_state.direction == SALES


def test_direction_clarify_answer_counts_as_one_round_and_can_finish() -> None:
    """方向追问**算一轮**（``round_used`` 加 1）；答完且必填齐了就结束，不许多问一句。"""
    pipeline, _counter, result = _run("矿泉水要十件", "dir-rounds")
    state = pipeline._clarify.begin(result.parse)  # noqa: SLF001
    assert state.round_used == 0

    answered = pipeline.answer_clarify(state, "进货", base=result.parse)
    assert answered.clarify_state is not None
    assert answered.clarify_state.round_used == 1
    # P/Q 本来就齐，方向也定了 → 会话结束，不再追问
    assert answered.clarify_state.finished is True
    assert answered.parse.route == "card"
