"""澄清：`POST /voice/sessions/{id}/clarify` —— **≤2 轮、一次 1 槽、点选不重调 AI**。

2 轮用尽的终点是 **MANUAL（数字键盘补录）**，不是 ``pending_task``
（后者是 R3 高风险待办、须 ``role=boss`` 在大屏二次确认的专属队列）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.models.voice import VoiceSession
from app.services.ledger import LedgerService, StoredCardPayload
from app.voice.clarify_fsm import MAX_CLARIFY_ROUNDS, ClarifyFSM, ClarifyState

router = APIRouter(prefix="/voice", tags=["clarify"])

#: 澄清用尽后的用户可见话术（与 clarify_fsm 保持同一句）
MANUAL_HINT = "没太听清，切到键盘补一下，点两下就好。"


class ClarifyAnswerRequest(BaseModel):
    """一次澄清回答（点选或补说）。"""

    model_config = ConfigDict(extra="forbid")

    round: int = Field(default=1, ge=1, le=MAX_CLARIFY_ROUNDS)
    slot: str | None = Field(default=None, max_length=8)
    value: str = Field(default="", max_length=200)


@router.post("/sessions/{session_id}/clarify", summary="回答澄清（静态话术，不调 AI）")
def answer_clarify(
    session_id: uuid.UUID,
    payload: ClarifyAnswerRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """本地结构化更新槽位；2 轮用尽 → ``route=manual``。

    Returns:
        包络 ``data: {route, slots, clarify, error_code, manual_entry_required}``
    """
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    voice_session = session.get(VoiceSession, session_id)
    if voice_session is None or voice_session.tenant_id != ids.tenant_id:
        raise AppError("E_NOT_FOUND", message="语音会话不存在或不属于当前租户")

    ledger = LedgerService(session)
    card = ledger._get_card_by_session(ids.tenant_id, session_id)  # noqa: SLF001
    payload_stored = (
        StoredCardPayload.from_json(card.slots)
        if card is not None
        else StoredCardPayload(intent_id="")
    )
    intent_id = payload_stored.intent_id
    if not intent_id:
        raise AppError("E_VALIDATION", message="该会话没有可澄清的解析结果，请重新说一遍")

    state = ClarifyState(
        intent_id=intent_id,
        slots=dict(payload_stored.slots),
        source=dict(payload_stored.source),
        round_used=int(voice_session.clarify_round or 0),
        pending_slot=payload.slot,
    )
    fsm = ClarifyFSM()
    updated = fsm.answer(state, payload.value, slot=payload.slot)

    voice_session.clarify_round = int(updated.round_used)

    if updated.exhausted:
        voice_session.state = "MANUAL_ENTRY"
        voice_session.error_code = "E_CLARIFY_LIMIT"
        session.flush()
        return JSONResponse(
            status_code=200,
            content=ok(
                {
                    "route": "manual",
                    "error_code": "E_CLARIFY_LIMIT",
                    "manual_entry_required": True,
                    "message_zh": MANUAL_HINT,
                    "round_used": updated.round_used,
                    "max_rounds": MAX_CLARIFY_ROUNDS,
                    "credit_consumed": False,
                    "note": "澄清用尽＝切数字键盘补录，不写待办、不扣次",
                }
            ),
        )

    prompt = updated.prompt
    voice_session.state = "CLARIFYING" if prompt is not None else voice_session.state
    session.flush()
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "route": "clarify" if prompt is not None else "resolved",
                "round_used": updated.round_used,
                "max_rounds": MAX_CLARIFY_ROUNDS,
                "slots": dict(updated.slots),
                "source": dict(updated.source),
                "clarify": None if prompt is None else prompt.model_dump(),
                "finished": updated.finished,
                "credit_consumed": False,
                "note": "点选/补说都只做本地结构化更新，0 次模型调用",
            }
        ),
    )
