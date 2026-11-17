"""澄清：`POST /voice/sessions/{id}/clarify` —— **≤2 轮、一次 1 槽、点选不重调 AI**。

2 轮用尽的终点是 **MANUAL（数字键盘补录）**，不是 ``pending_task``
（后者是 R3 高风险待办、须 ``role=boss`` 在大屏二次确认的专属队列）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.api.v1.voice import build_card_after_clarify
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.models.voice import VoiceSession
from app.services.ledger import LedgerService, StoredCardPayload
from app.voice.clarify_fsm import MAX_CLARIFY_ROUNDS, ClarifyFSM, ClarifyState
from app.voice.output_schema import ParseResult

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
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """本地结构化更新槽位；2 轮用尽 → ``route=manual``；补齐后**当场出卡**。

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
    slots = dict(payload_stored.slots)
    source = dict(payload_stored.source)
    snapshot: dict | None = None

    if not intent_id:
        # **没有卡**（`route=clarify` 分支本来就不建卡）→ 从**解析快照**接着走。
        #
        # 没有这一步，用户点完选项会撞 422，什么也不会发生：
        # 「说不全」是这套产品的通用分支（缺必填槽、置信度低都走这里），
        # 一旦走不通，PRD 写的「最多问 2 句」实际一句都问不成。
        snapshot = ledger.parse_snapshot(tenant_id=ids.tenant_id, session_id=session_id)
        if snapshot is not None:
            intent_id = str(snapshot.get("intent") or "")
            slots = dict(snapshot.get("slots") or {})
            source = {str(k): str(v) for k, v in (snapshot.get("source") or {}).items()}

    if not intent_id:
        raise AppError(
            "E_VALIDATION",
            message="该会话没有可澄清的解析结果，请重新说一遍",
            data={"session_id": str(session_id)},
        )

    state = ClarifyState(
        intent_id=intent_id,
        slots=slots,
        source=source,
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

    # 槽位补齐了、而且**这一轮本来就没有卡** → 当场把卡补建出来。
    #
    # 没有这一步的话，用户点完选项只拿到一句 ``resolved``，手上什么都没有：
    # 澄清等于白做。补建走同一条 ``create_card``，所以
    # 「卡片生成成功才扣 1 次」的口径不变（澄清阶段不扣次）。
    if prompt is None and card is None:
        built = _build_card(
            session=session,
            request=request,
            settings=settings,
            context=context,
            ids=ids,
            session_id=session_id,
            state=updated,
            snapshot=snapshot,
            voice_session=voice_session,
        )
        if built is not None:
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        "route": "card",
                        "card": built.as_dict(),
                        "slots": dict(updated.slots),
                        "source": dict(updated.source),
                        "round_used": updated.round_used,
                        "max_rounds": MAX_CLARIFY_ROUNDS,
                        "finished": True,
                        "credit_consumed": built.billed,
                        "note": "澄清补齐后当场出卡；卡片生成成功才扣 1 次",
                    }
                ),
            )

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


def _build_card(
    *,
    session: Any,
    request: Request,
    settings: Any,
    context: Any,
    ids: Any,
    session_id: uuid.UUID,
    state: ClarifyState,
    snapshot: dict | None,
    voice_session: Any,
) -> Any | None:
    """澄清补齐后补建确认卡；建不出来就返回 ``None``（调用方转手动补录）。"""
    try:
        parse = ParseResult(
            intent=state.intent_id,
            slots=dict(state.slots),
            source=dict(state.source),
            confidence=float((snapshot or {}).get("confidence") or 0.5),
            channel="template",
            # **计算轨迹必须一起带回来**：`TA`（合计）这类槽位在 source 里标着
            # ``computed``，而 ``computed`` 槽位必须有对应计算式，否则
            # ``validate_sources`` 判定「source 标注违规」→ 建卡直接抛错 →
            # 澄清补齐了却拿不到卡（用户点了选项，屏幕上什么都不发生）。
            #
            # 轨迹在解析那一步就写进 ``voice_log.parse_result`` 快照了，
            # 这里只是没把它取出来用——不是「没有轨迹」。
            calc_traces=[str(trace) for trace in (snapshot or {}).get("calc_traces") or []],
        )
    except Exception:  # noqa: BLE001 - 槽位被改坏时不该把整个请求打成 500
        return None
    creation = build_card_after_clarify(
        session=session,
        request=request,
        settings=settings,
        context=context,
        ids=ids,
        session_id=session_id,
        parse=parse,
        asr_text=str((snapshot or {}).get("asr_text") or ""),
        asr_confidence=float((snapshot or {}).get("asr_confidence") or 0.0),
    )
    if creation is None:
        voice_session.state = "MANUAL_ENTRY"
        voice_session.error_code = "E_CLARIFY_LIMIT"
        session.flush()
        return None
    voice_session.state = "AWAIT_CONFIRM"
    session.flush()
    return creation
