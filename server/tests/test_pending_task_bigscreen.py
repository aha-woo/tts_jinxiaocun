"""R3 待办：**语音只生成待办，执行只能大屏 + boss + 二次确认**（验收②③ / H8 4.3）。

为什么这条要单独三档校验
--------------------
R3 是「合并往来单位」「设换算系数」「导出全部数据」这类**错了很难回滚**的动作。
语音识别再准，也不该让它直接执行——嘈杂环境里听错一个字，就是两家的账合并。

所以判定顺序是固定的、且每一档都能单独失败：

1. **角色**（谁能做）：必须 ``role=boss``，否则 ``E_ROLE_DENIED``(1302)；
2. **设备**（在哪做）：必须 ``device=pc_bigscreen``（电脑版微信大屏），
   手机端与语音端一律 ``E_R3_PC_REQUIRED``(1206)；
3. **二次确认**（怎么算定了）：缺了就 ``E_VALIDATION``(422)。

并且：**没有任何路径**能让 R3 动作通过语音卡片直接写库——
``LedgerService.create_card`` / ``ConfirmCardBuilder`` 都必须当场拒绝。
"""

from __future__ import annotations

import uuid

import pytest

from app.common.errors import AppError
from app.domain.intent_registry import default_registry
from app.services.ledger import LedgerService
from app.services.pending_task import (
    BIG_SCREEN_DEVICE,
    R3_ROLE,
    PendingTaskService,
    TaskView,
)
from app.voice.card_builder import CardBuildError, ConfirmCardBuilder
from app.voice.impact_preview import PreviewBundle
from t04_fixtures import parse_of, seed, spec_of  # noqa: F401  pytest 夹具

#: 三个 R3 动作（全部 ``whitelist_blocked=true``）
R3_INTENTS = ("partner.merge", "product.unit.set", "report.export")


def _task(seed_obj, session, intent_id: str = "partner.merge") -> TaskView:
    """在给定会话里生成一条 R3 待办。"""
    return PendingTaskService(session).create(
        tenant_id=seed_obj.tenant_id,
        session_id=uuid.uuid4(),
        intent_id=intent_id,
        payload={"slots": {"DOC": "上一单"}, "source": {"DOC": "asr"}},
        member_id=seed_obj.member_id,
    )


# ------------------------------------------------- ① 没有语音直写旁路
@pytest.mark.parametrize("intent_id", R3_INTENTS)
def test_r3_cannot_build_card_or_write_directly(seed, intent_id: str) -> None:
    """R3 动作：构造卡片直接抛；落账服务返 ``E_R3_BLOCKED``(1205)。"""
    spec = spec_of(intent_id)
    assert spec.whitelist_blocked is True

    with pytest.raises(CardBuildError):
        ConfirmCardBuilder().build(
            parse_of(intent_id),
            card_id="c",
            session_id="s",
            asr_text="随便说说",
            asr_confidence=0.9,
        )

    with seed.database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            LedgerService(session).create_card(
                tenant_id=seed.tenant_id,
                store_id=seed.store_id,
                member_id=seed.member_id,
                session_id=uuid.uuid4(),
                parse=parse_of(intent_id),
                asr_text="随便说说",
                asr_confidence=0.9,
                lines=[],
                preview=PreviewBundle(),
            )
    assert excinfo.value.resolved_code == "E_R3_BLOCKED"
    assert excinfo.value.spec().numeric == 1205
    assert (excinfo.value.data or {}).get("next") == "pending_task"


def test_r3_task_creation_is_idempotent(seed) -> None:
    """同一会话同一意图只生成一条待办（重复解析不会把审批箱刷屏）。"""
    session_id = uuid.uuid4()
    with seed.database.session(context=seed.context) as session:
        service = PendingTaskService(session)
        first = service.create(
            tenant_id=seed.tenant_id,
            session_id=session_id,
            intent_id="partner.merge",
            payload={"slots": {"DOC": "上一单"}},
            member_id=seed.member_id,
        )
        second = service.create(
            tenant_id=seed.tenant_id,
            session_id=session_id,
            intent_id="partner.merge",
            payload={"slots": {"DOC": "上一单"}},
            member_id=seed.member_id,
        )
        listed = service.list_pending(tenant_id=seed.tenant_id)
    assert first.task_id == second.task_id
    assert len(listed) == 1
    assert listed[0].requires_pc is True
    assert listed[0].required_role == R3_ROLE == "boss"
    assert "电脑版微信大屏" in listed[0].as_dict()["hint_zh"]


# ------------------------------------------------- ② 三档校验，顺序固定
def test_non_boss_role_is_denied(seed) -> None:
    """非老板 → ``E_ROLE_DENIED``(1302)，提示里要写清需要的角色。"""
    with seed.database.session(context=seed.context) as session:
        task = _task(seed, session)
    with seed.database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            PendingTaskService(session).decide(
                tenant_id=seed.tenant_id,
                task_id=uuid.UUID(task.task_id),
                approve=True,
                role="manager",
                device=BIG_SCREEN_DEVICE,
                second_confirm=True,
            )
    assert excinfo.value.resolved_code == "E_ROLE_DENIED"
    assert excinfo.value.spec().numeric == 1302
    assert (excinfo.value.data or {}).get("required_role") == "boss"


def test_phone_cannot_execute_r3(seed) -> None:
    """手机端（非大屏）→ ``E_R3_PC_REQUIRED``(1206)，且**不许**引导到外部浏览器/H5。"""
    with seed.database.session(context=seed.context) as session:
        task = _task(seed, session)
    with seed.database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            PendingTaskService(session).decide(
                tenant_id=seed.tenant_id,
                task_id=uuid.UUID(task.task_id),
                approve=True,
                role="boss",
                device="phone",
                second_confirm=True,
            )
    assert excinfo.value.resolved_code == "E_R3_PC_REQUIRED"
    assert excinfo.value.spec().numeric == 1206
    message = excinfo.value.resolved_message
    assert "电脑版微信大屏" in message
    assert "http" not in message and "浏览器" not in message, "把用户往网页端引了（本项目没有网页端）"


def test_second_confirm_is_mandatory(seed) -> None:
    """大屏 + boss 但没二次确认 → ``E_VALIDATION``（差一项都不给执行）。"""
    with seed.database.session(context=seed.context) as session:
        task = _task(seed, session)
    with seed.database.session(context=seed.context) as session:
        with pytest.raises(AppError) as excinfo:
            PendingTaskService(session).decide(
                tenant_id=seed.tenant_id,
                task_id=uuid.UUID(task.task_id),
                approve=True,
                role="boss",
                device=BIG_SCREEN_DEVICE,
                second_confirm=False,
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    with seed.database.session(context=seed.context) as session:
        assert PendingTaskService(session).list_pending(tenant_id=seed.tenant_id)[0].status == "pending"


def test_bigscreen_boss_with_second_confirm_can_approve(seed) -> None:
    """大屏 + boss + 二次确认 → 通过；重复审批幂等返回。"""
    with seed.database.session(context=seed.context) as session:
        task = _task(seed, session)
    with seed.database.session(context=seed.context) as session:
        decision = PendingTaskService(session).decide(
            tenant_id=seed.tenant_id,
            task_id=uuid.UUID(task.task_id),
            approve=True,
            role="boss",
            device=BIG_SCREEN_DEVICE,
            second_confirm=True,
            member_id=seed.member_id,
        )
    assert decision.approved is True
    assert decision.status == "approved"
    # 真正的业务执行由大屏侧调对应域服务完成；本服务只负责「准不准做」
    assert decision.executed is False

    with seed.database.session(context=seed.context) as session:
        again = PendingTaskService(session).decide(
            tenant_id=seed.tenant_id,
            task_id=uuid.UUID(task.task_id),
            approve=True,
            role="boss",
            device=BIG_SCREEN_DEVICE,
            second_confirm=True,
        )
    assert "幂等" in again.detail
    with seed.database.session(context=seed.context) as session:
        assert PendingTaskService(session).list_pending(tenant_id=seed.tenant_id) == []


def test_rejecting_a_task_keeps_it_out_of_the_queue(seed) -> None:
    """驳回：状态变 ``rejected``，从待办列表消失（审批箱要能清空）。"""
    with seed.database.session(context=seed.context) as session:
        task = _task(seed, session)
    with seed.database.session(context=seed.context) as session:
        decision = PendingTaskService(session).decide(
            tenant_id=seed.tenant_id,
            task_id=uuid.UUID(task.task_id),
            approve=False,
            role="boss",
            device=BIG_SCREEN_DEVICE,
            second_confirm=True,
        )
    assert decision.approved is False and decision.status == "rejected"
    with seed.database.session(context=seed.context) as session:
        assert PendingTaskService(session).list_pending(tenant_id=seed.tenant_id) == []


def test_clarify_exhaustion_never_creates_a_pending_task(seed) -> None:
    """澄清用尽 ≠ 待办：这是 R3 高风险动作的专属队列，不能被「听不清」污染。"""
    from app.voice.clarify_fsm import CLARIFY_LIMIT_MESSAGE

    with seed.database.session(context=seed.context) as session:
        tasks_before = PendingTaskService(session).list_pending(tenant_id=seed.tenant_id)
    assert tasks_before == []
    assert "键盘" in CLARIFY_LIMIT_MESSAGE, "澄清用尽的话术应引导到数字键盘补录"


def test_r3_intents_are_exactly_the_frozen_three() -> None:
    """R3 白名单只有这三个（多一个就是给某个动作悄悄开了旁路）。"""
    blocked = [spec.intent_id for spec in default_registry().all() if spec.whitelist_blocked]
    assert tuple(blocked) == R3_INTENTS
    assert BIG_SCREEN_DEVICE == "pc_bigscreen"
