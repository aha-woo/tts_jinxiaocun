"""38 个 intent **逐个**断言路由，不做抽查（验收②）。

一条铁律 + 三重对齐
------------------
**任何写操作必经确认卡片。R0 只读不建卡/不写库/不扣次；R1/R2 必须卡片；
R3 只生成待办、且只能大屏 + role=boss + 二次确认；没有语音直写旁路。**

光断言「``route_of_spec`` 返回了什么」是不够的（那只是把 if 抄了一遍）。
所以每个意图都做三重对齐：

1. ``route_of_spec`` 的结论 == 用契约字段独立重算的结论（防止判定顺序写错）；
2. **真实行为**必须与路由一致：
   - ``card``：真的能构造出 7 区块卡片，且建卡时真的扣了 1 次；
   - ``answer`` / ``static``：卡片构造器抛 ``CardBuildError``，落账服务抛 ``E_VALIDATION``；
   - ``pending_task``：卡片构造器抛 ``CardBuildError``，落账服务抛 ``E_R3_BLOCKED``；
3. 落账服务对**每一种**非 ``card`` 路由都不许写出任何一行业务数据。

最后一条测试汇总打印 R0/R1/R2/R3 各多少条（交付格式第⑦项要的就是这些数字）。
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.common.errors import AppError
from app.domain.intent_registry import default_registry
from app.models.doc import DocHeader
from app.services.ledger import LedgerService
from app.voice.card_builder import BLOCK_KEYS, CardBuildError, ConfirmCardBuilder
from app.voice.impact_preview import PreviewBundle
from app.voice.routing import (
    ROUTE_ANSWER,
    ROUTE_CARD,
    ROUTE_PENDING_TASK,
    ROUTE_STATIC,
    route_bills_credit,
    route_builds_card,
    route_for_intent,
    route_of_spec,
    route_table,
    route_writes_db,
)
from t04_fixtures import (
    count_rows,
    create_card_for,
    parse_of,
    seed,  # noqa: F401  pytest 夹具
    spec_of,
)

#: 全部 38 个意图（顺序取契约 ``seq``）
ALL_INTENTS: tuple[str, ...] = tuple(spec.intent_id for spec in default_registry().all())

#: 非 card 路由走到落账服务时**必须**报的错误码（不许静默通过）
EXPECTED_BLOCK_CODE = {
    ROUTE_ANSWER: "E_VALIDATION",
    ROUTE_STATIC: "E_VALIDATION",
    ROUTE_PENDING_TASK: "E_R3_BLOCKED",
}


def _expected_route_of(spec) -> str:
    """用契约字段独立重算一遍路由（不调用 routing.py，用于比对）。"""
    if spec.intent_id in {"sys.help", "sys.example"}:
        return ROUTE_STATIC
    if spec.whitelist_blocked:
        return ROUTE_PENDING_TASK
    if spec.risk_level == "R0":
        return ROUTE_ANSWER
    return ROUTE_CARD


@pytest.mark.parametrize("intent_id", ALL_INTENTS)
def test_every_intent_route_matches_real_behaviour(seed, intent_id: str) -> None:
    """逐个断言：路由结论、卡片构造、落账服务三者必须一致。"""
    spec = spec_of(intent_id)
    route = route_of_spec(spec)

    # ① 判定点与独立重算一致
    assert route == _expected_route_of(spec), f"{intent_id} 的路由判定与契约字段矛盾"
    assert route_for_intent(intent_id) == route

    # ② 行为对齐
    if route == ROUTE_CARD:
        with seed.database.session(context=seed.context) as session:
            _, creation = create_card_for(seed, session=session, intent_id=intent_id)
            assert set(creation.view.blocks()) == set(BLOCK_KEYS), "卡片区块缺失"
            assert len(creation.view.blocks()) == 7
        assert creation.billed is True, f"{intent_id} 建卡却没收次"
        assert route_bills_credit(route) is True
        assert route_writes_db(route) is True
        assert route_builds_card(route) is True
        return

    # 非 card：卡片构造器必须拒绝，落账服务必须报对应错误码
    with pytest.raises(CardBuildError) as build_error:
        ConfirmCardBuilder().build(
            parse_of(intent_id),
            card_id="c-1",
            session_id="s-1",
            asr_text="随便说说",
            asr_confidence=0.95,
        )
    assert spec.intent_id in str(build_error.value) or "R0" in str(build_error.value) or "R3" in str(build_error.value)

    with seed.database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            LedgerService(session).create_card(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                member_id=seed.member_id,
                session_id=_uuid(intent_id),
                parse=parse_of(intent_id),
                asr_text="随便说说",
                asr_confidence=0.95,
                lines=[],
                preview=PreviewBundle(),
            )
    assert excinfo.value.resolved_code == EXPECTED_BLOCK_CODE[route]
    assert route_bills_credit(route) is False
    assert route_writes_db(route) is False
    assert route_builds_card(route) is False

    # ③ 一行业务数据都不许写出来
    assert count_rows(seed.database, DocHeader) == 0, f"{intent_id} 通过非 card 路由写出了单据"


def _uuid(text_value: str):
    """由 intent_id 派生一个稳定 UUID（同一意图每次跑都一致，便于对比）。"""
    import uuid as _uuid_mod

    return _uuid_mod.uuid5(_uuid_mod.NAMESPACE_URL, f"vil-route/{text_value}")


def test_route_distribution_is_exactly_38_and_printed() -> None:
    """路由分布汇总：38 条全覆盖，数字打印出来供交付报告引用。"""
    table = route_table()
    counts = Counter(table.values())
    by_risk = Counter(spec.risk_level for spec in default_registry().all())

    print("\n[38 intent 路由矩阵]")
    print(f"  总意图数 = {len(table)}（契约 intents.json count = {len(default_registry().all())}）")
    print(f"  R0 只读 = {by_risk['R0']}（其中 static {counts[ROUTE_STATIC]} / answer {counts[ROUTE_ANSWER]}）")
    print(f"  R1 常规 = {by_risk['R1']}")
    print(f"  R2 高风险 = {by_risk['R2']}")
    print(f"  R3 大屏 = {by_risk['R3']}（全部 pending_task = {counts[ROUTE_PENDING_TASK]}）")
    print(f"  必须出卡（R1+R2） = {counts[ROUTE_CARD]}")
    print(f"  不建卡不扣次 = {counts[ROUTE_ANSWER] + counts[ROUTE_STATIC] + counts[ROUTE_PENDING_TASK]}")

    assert len(table) == 38
    assert counts[ROUTE_STATIC] == 2, "静态意图只有 sys.help / sys.example"
    assert counts[ROUTE_PENDING_TASK] == 3, "R3 白名单只有 partner.merge / product.unit.set / report.export"
    assert counts[ROUTE_ANSWER] == by_risk["R0"] - counts[ROUTE_STATIC]
    assert counts[ROUTE_CARD] == by_risk["R1"] + by_risk["R2"]
    assert counts[ROUTE_CARD] + counts[ROUTE_ANSWER] + counts[ROUTE_STATIC] + counts[ROUTE_PENDING_TASK] == 38


def test_r3_intents_have_no_direct_write_path() -> None:
    """R3 三个动作：``side_effect.writes_db`` 必须是 ``false``，落库实体只能含 ``pending_task``。"""
    r3 = [spec for spec in default_registry().all() if spec.whitelist_blocked]
    assert [spec.intent_id for spec in r3] == ["partner.merge", "product.unit.set", "report.export"]
    for spec in r3:
        assert spec.side_effect.get("writes_db") is False, f"{spec.intent_id} 号称 R3 却能直接写库"
        assert "pending_task" in (spec.side_effect.get("entities") or []), (
            f"{spec.intent_id} 的落库实体里没有 pending_task"
        )
        assert spec.confirm_policy == "C3"


def test_r0_intents_never_bill_or_write() -> None:
    """R0 十三个动作：全部 ``writes_db=false``，且撤销规格里不存在撤销。"""
    r0 = [spec for spec in default_registry().all() if spec.risk_level == "R0"]
    assert len(r0) == 13
    for spec in r0:
        assert spec.side_effect.get("writes_db") is False
        assert spec.reversal.get("allowed") is False, f"{spec.intent_id} 没写过却允许撤销"
        assert route_for_intent(spec.intent_id) in {ROUTE_ANSWER, ROUTE_STATIC}
