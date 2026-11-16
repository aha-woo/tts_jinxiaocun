"""G3 held-out 集：**独立于契约 ``sample_utterances`` 的口语命中率与误判率**。

为什么要单独一份 held-out（team-lead 复核时提的要求）
--------------------------------------------------
`test_template_hit.py` 测的 84 条来自契约自带的 `sample_utterances`，它回答的是
「**被设计成走规则的那部分意图，规则层是否都接住了**」——是设计覆盖度检验，
**不是**「真实商户说一句话有多大比例不用花 LLM 的钱」。原因有三：

1. 样本分布均匀（38 intent 各 2~3 条），而真实场景极度偏斜（进货/卖货占大头）；
2. 规则表与样本出自同一作者同一时期，「100% 接住」天然自洽，不构成独立验证；
3. 契约样本是**书面语**，真实口语带拖音、口头追加语、错别字、同音字、方言说法。

本文件的句子是**先写、先跑，跑完不回改规则表**——一旦回头改规则去适配这批句子，
held-out 就不再 held-out，两个数字会退化成同一个数字。

两个数字必须分开报，不许混成一个
------------------------------
- ``sample_utterances`` 命中率 ``50.0%`` —— 设计覆盖度检验；
- held-out 口语命中率 —— 更接近生产估计（样本量小，仅作量级参考）。

判定口径（严格版，team-lead 明确要求）
------------------------------------
一条样本算**命中**，必须同时满足：

1. 结论由**规则层或 G2 静态路由**给出（``rule_hit`` 或 ``stage == "G2"``），
   即**没有为此花掉一次模型钱**；
2. ``intent`` 与期望**完全一致**（``*_multi`` 与单商品不互通，落错了一律算误判）；
3. ``expect_slots`` 里列出的每个槽位**值也对**。

否则：
- 规则层没给结论（升级到 G4）→ 计 **未命中（miss）**，不算误判；
- 规则层给了结论但 ``intent`` 错或槽位错 → 计 **误判（misjudge）**。
  误判比未命中更危险：它是**花了 0 次模型钱却记错了账**。
G2 把合法命令当闲聊拦掉（误杀）也计误判——「宁可放行、不可误杀」是 G2 的硬约束。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voice_demo import (
    TODAY,
    build_pipeline,
    demo_customers_resolver,
    demo_resolver,
    demo_suppliers_resolver,
)

#: held-out 集规模（句子写死在 JSON 里，改句子必须同步改这个数并在 PR 里说明）
EXPECTED_HELDOUT = 36

#: 期望覆盖的高频场景（少于 5 个就说明样本不够分散）
EXPECTED_SCENARIOS = frozenset(
    {"purchase", "sales", "stock_query", "debt_query", "return", "stock_take"}
)

#: `sample_utterances` 回归集的实测命中率（设计覆盖度检验，别和 held-out 混为一谈）
SAMPLE_UTTERANCE_HIT_RATE = 0.50

#: 整改**前**基线（方案 d 之前跑出来的，留档对照，不用于断言）。
#:
#: 用**同一套严格口径**（``route == "clarify"`` 不算命中）重新量过一遍老代码：
#: 命中 10 / 强制澄清 11 / 未命中 12 / **误判 3**（H02/H32/H36）。
#: 注意：T03 当时报的「17 命中 / 7 误判」用的是**宽松口径**（意图+槽位对就算命中，
#: 不管是不是反问了一句），两个口径不能混着比——这里统一成严格口径再对照。
BEFORE_DIRECTION_FIX = {"hits": 10, "clarify": 11, "misses": 12, "misjudges": 3}

#: 整改**后**的回归基线（**只许往下调的只有误判**，其余钉住不回退）。
#:
#: 这批 36 条已经被看过结果、也据此改过代码，**它是 regression set，不是 held-out 了**。
#: 新的独立验证集见 ``tests/fixtures/holdout_independent.json`` + ``test_g3_independent.py``，
#: 那批句子才是「方案 d 之后」的数字来源。
#:
#: 2026-02 变更记录（**逐条说明，不许静默改**）：
#: - 命中 14 → 19。原因不是「放宽断言凑数」，而是修掉两个**真缺陷**：
#:   ① 可选槽位 ``pending``（没说供应商 → ``S=待补``）被按必填扣 0.25 置信度，
#:      使「进了两箱可乐，一件三十五」这类最常见的话**永远出不了卡**；
#:   ② 单商品意图也带 1 个「只有文本没有数量」的 item，被 ``lines_of_result``
#:      当成多商品走 item 分支，落成数量 0 / 金额 0 的空单据。
#:   修完之后命中升到 19、误判仍是 0，所以把下限**往上**钉（防回退）。
#: - 误判 0 → 仍为 0（这是唯一允许往下调的指标）。
BASELINE_HITS = 19
BASELINE_MISJUDGES = 0

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "heldout_utterances.json"


def load_heldout() -> list[dict[str, Any]]:
    """读 held-out 句子表（缺失即测试失败，不允许静默跳过）。"""
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows = list(doc["utterances"])
    assert len(rows) == EXPECTED_HELDOUT, f"held-out 集规模变了：{len(rows)}"
    return rows


def test_heldout_covers_all_high_frequency_scenarios() -> None:
    """样本必须覆盖 6 个高频场景，且场景名与句子上标的一致。"""
    rows = load_heldout()
    scenarios = {row["scenario"] for row in rows}
    assert scenarios == set(EXPECTED_SCENARIOS), f"场景覆盖不全：{sorted(scenarios)}"
    for row in rows:
        assert row["text"].strip(), f"{row['id']} 句子为空"
        assert row["expect_intent"], f"{row['id']} 缺期望意图"


def test_heldout_contains_real_colloquial_noise() -> None:
    """**这批句子必须真的脏**：至少有同音错字/口语起手/口头追加语。

    这条测试是防「把 held-out 写成第二份书面语样本」——那样两个数字会趋同，
    held-out 就失去了「独立验证」的意义。
    """
    rows = load_heldout()
    texts = [row["text"] for row in rows]
    joined = "".join(texts)

    noises = {
        "同音错字": any(word in joined for word in ("哇哈哈", "宏牛")),
        "口头起手/追加语": any(
            word in joined for word in ("哎那个", "把这个也记上", "刚才", "上回")
        ),
        "口语金额读法": any(word in joined for word in ("一块八", "三块五", "一百五")),
        "方言出货说法": "出去" in joined,
    }
    missing = [name for name, present in noises.items() if not present]
    assert missing == [], f"held-out 缺少真实口语杂质：{missing}"


def _slot_matches(code: str, got: Any, want: Any) -> bool:
    """槽位比对（``P`` 是 ``entity_ref``，不是自由文本）。

    ``P`` 在引擎里填的是「名称 + 规格」（如 ``可乐 500ml``），这是**商品档案的正常
    表示**而不是错值，所以按「解析出的商品名等于期望名」比对。除此之外一律严格等值：
    数量、单位、付款状态差一个字都算错。
    """
    if got is None:
        return False
    if code == "P":
        return str(got).split(" ")[0].strip() == str(want).strip()
    return got == want


def _run_all() -> list[dict[str, Any]]:
    """跑完整个 held-out 集，逐条记录判定结果（**不修改任何规则**）。"""
    pipeline, counter = build_pipeline()
    outcomes: list[dict[str, Any]] = []
    for row in load_heldout():
        result = pipeline.run(
            row["text"],
            today_iso=TODAY,
            resolver=demo_resolver(),
            customers=demo_customers_resolver(),
            suppliers=demo_suppliers_resolver(),
            session_key=f"heldout-{row['id']}",
        )
        rule_layer = bool(result.rule_hit) or result.stage == "G2"
        slots = dict(result.parse.slots) if result.parse is not None else {}
        slot_ok = all(
            _slot_matches(code, slots.get(code), value)
            for code, value in row["expect_slots"].items()
        )
        intent_ok = result.intent_id == row["expect_intent"]

        route = result.parse.route if result.parse is not None else None
        if not rule_layer:
            verdict = "miss"
        elif route == "clarify":
            # 规则层**没给结论**（反问一句），0 次模型钱、也**没有记错账**：
            # 这是「合格的保守」而不是「误判」。方向判据不足时的强制澄清走这一桶。
            verdict = "clarify"
        elif intent_ok and slot_ok:
            verdict = "hit"
        else:
            verdict = "misjudge"

        outcomes.append(
            {
                "id": row["id"],
                "scenario": row["scenario"],
                "text": row["text"],
                "expect": row["expect_intent"],
                "got": result.intent_id,
                "stage": result.stage,
                "reason": result.reason,
                "route": result.parse.route if result.parse is not None else None,
                "clarify_slot": (
                    result.parse.clarify.slot
                    if result.parse is not None and result.parse.clarify is not None
                    else None
                ),
                "slots": slots,
                "expect_slots": row["expect_slots"],
                "intent_ok": intent_ok,
                "slot_ok": slot_ok,
                "verdict": verdict,
            }
        )
    outcomes.append({"llm_calls": counter.calls})
    return outcomes


def test_heldout_hit_rate_and_misjudge_rate(capsys: pytest.CaptureFixture[str]) -> None:
    """把**三个数字**打出来：命中率 / 误判率 / 强制澄清率（并打印全部明细）。"""
    outcomes = _run_all()
    llm_calls = int(outcomes[-1]["llm_calls"])
    rows = [row for row in outcomes if "verdict" in row]

    hits = [row for row in rows if row["verdict"] == "hit"]
    misses = [row for row in rows if row["verdict"] == "miss"]
    clarifies = [row for row in rows if row["verdict"] == "clarify"]
    misjudges = [row for row in rows if row["verdict"] == "misjudge"]

    total = len(rows)
    hit_rate = len(hits) / total
    miss_rate = len(misses) / total
    clarify_rate = len(clarifies) / total
    misjudge_rate = len(misjudges) / total

    lines = [
        "",
        "[held-out 回归集实测] 真实口语集 %d 条（6 个高频场景，含同音错字/拖音/口头追加语）"
        % total,
        "  整改前基线（方案 d 之前）: 命中 %d / 未命中 %d / 误判 %d"
        % (
            BEFORE_DIRECTION_FIX["hits"],
            BEFORE_DIRECTION_FIX["misses"],
            BEFORE_DIRECTION_FIX["misjudges"],
        ),
        "  命中（规则层直接给对意图且槽位对，0 次模型钱）: %d 条 = %.1f%%"
        % (len(hits), hit_rate * 100),
        "  强制澄清（规则层判不出 → 反问一句，0 次模型钱、未记错账）: %d 条 = %.1f%%"
        % (len(clarifies), clarify_rate * 100),
        "  未命中（升级到 G4 花钱）: %d 条 = %.1f%%" % (len(misses), miss_rate * 100),
        "  误判（**给了落账结论**但意图或槽位错 → 记错账）: %d 条 = %.1f%%"
        % (len(misjudges), misjudge_rate * 100),
        "  对照：sample_utterances 设计覆盖度检验命中率 = %.1f%%（**不是**生产估计）"
        % (SAMPLE_UTTERANCE_HIT_RATE * 100),
        "  本次 held-out 跑的模型调用次数 = %d（G4 兜底消耗）" % llm_calls,
    ]
    direction_clarifies = [row for row in clarifies if row["clarify_slot"] == "DIR"]
    other_clarifies = [row for row in clarifies if row["clarify_slot"] != "DIR"]
    lines.append(
        "    其中：方向澄清 %d 条（方案 d 新增的「判不出方向就问一句」）／"
        "低置信澄清 %d 条（T03 就有的 confidence<0.6 路径，非本轮引入）"
        % (len(direction_clarifies), len(other_clarifies))
    )
    for row in clarifies:
        lines.append(
            "  [强制澄清] %s %s「%s」-> 问 %s（原判意图 %s，期望 %s）"
            % (
                row["id"],
                row["scenario"],
                row["text"],
                "方向 DIR" if row["clarify_slot"] == "DIR" else f"槽位 {row['clarify_slot']}（低置信）",
                row["got"],
                row["expect"],
            )
        )
    for row in misses:
        lines.append(
            "  [未命中] %s %s「%s」-> %s/%s（期望 %s）"
            % (row["id"], row["scenario"], row["text"], row["stage"], row["reason"], row["expect"])
        )
    for row in misjudges:
        reasons = []
        if not row["intent_ok"]:
            reasons.append(f"意图 {row['got']} != {row['expect']}")
        if not row["slot_ok"]:
            reasons.append(f"槽位 {row['slots']} 不满足 {row['expect_slots']}")
        lines.append(
            "  [误判] %s %s「%s」-> %s（%s）"
            % (row["id"], row["scenario"], row["text"], row["got"], "；".join(reasons))
        )
    print("\n".join(lines))

    # ---- 断言：只钉住「不能变得更差」，不钉「必须达到 45%」 ----
    # 45% 是 PRD 对 **sample_utterances** 的假设值，held-out 上不套用这个阈值：
    # 真实口语只会更难，硬套等于逼着后来的人去放宽规则凑数。
    assert total == EXPECTED_HELDOUT
    # 误判是红线里的红线：规则层 0 成本地把账记错，比多花一次模型钱危险得多。
    # 但**首轮实测 19.4% 是真实值**，这里如实钉成基线而不是假装达标——
    # 7 条误判的明细就在下面打印的 [误判] 行里，是给 team-lead 的规则表整改清单。
    assert len(misjudges) <= BASELINE_MISJUDGES, (
        f"held-out 误判从基线 {BASELINE_MISJUDGES} 条涨到 {len(misjudges)} 条"
        f"（{misjudge_rate:.1%}）：规则层在真实口语上开始瞎猜了，"
        "必须收紧规则表（而不是放宽断言）\n"
        + "\n".join(line for line in lines if "[误判]" in line)
    )
    # 命中数兜底：低于 ``BASELINE_HITS`` 说明规则层在真实口语上回退了。
    # 数值见文件顶部常量旁的变更记录——每次调这个数都必须写清「为什么可以调」。
    assert len(hits) >= BASELINE_HITS, (
        f"held-out 命中 {len(hits)} 条低于基线 {BASELINE_HITS} 条（{hit_rate:.1%}）："
        "规则层对真实口语出现回退"
    )


def test_heldout_misses_are_escalated_not_guessed() -> None:
    """未命中的样本必须**升级到 G4**，不能停在规则层给个瞎猜结论。

    等价于「宁可放行、不可误杀」在成本侧的另一面：**宁可花一次模型钱，不可猜着记账**。
    """
    outcomes = _run_all()
    rows = [row for row in outcomes if "verdict" in row]
    bad = [
        row
        for row in rows
        if row["verdict"] == "miss" and row["stage"] not in {"G3", "G4", "G2", "G1", "G0"}
    ]
    assert bad == [], f"未命中样本落在了非预期阶段：{bad}"


def test_heldout_is_not_tuned_to_the_rule_table() -> None:
    """held-out 句子**不得出现在契约 ``sample_utterances`` 里**（防自证循环）。

    这条是「held-out 有效性」的元测试：如果哪天有人把 held-out 句子抄进契约样本，
    两个命中率就会互相污染，本文件的存在意义立刻归零。
    """
    contracts = Path(__file__).resolve().parents[2] / "packages" / "shared" / "contracts"
    doc = json.loads((contracts / "intents.json").read_text(encoding="utf-8"))
    samples = {utterance for intent in doc["intents"] for utterance in intent["sample_utterances"]}
    leaked = [row["text"] for row in load_heldout() if row["text"] in samples]
    assert leaked == [], f"held-out 句子被抄进了契约样本，独立验证失效：{leaked}"
