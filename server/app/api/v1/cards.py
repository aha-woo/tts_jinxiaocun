"""卡片读取与编辑：`GET /voice/cards/{id}`、`PATCH /voice/cards/{id}/edit`、`GET /voice/cards/pending`。

两条要点
-------
1. **GET 只有 7 区块 + meta**，不多不少（``ConfirmCardView.blocks()`` 的键集合是硬约束）；
2. **PATCH 不重调 AI**：改槽位 = 本地重算 + 重新只读预演，**从不重新解析原话**
   （验收⑥由 ``test_correction_no_llm.py`` 用 ``CountingLlmProvider.calls`` 钉住）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, runtime_for, WRITE_ROLES
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep, get_database
from app.services.ledger import LedgerService, StoredCardPayload
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.services.voice_runtime import lines_of_result

router = APIRouter(prefix="/voice/cards", tags=["cards"])


class SlotUpdate(BaseModel):
    """一个槽位的修改。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=8)
    value: Any = None


class CardEditRequest(BaseModel):
    """编辑卡片（只改结构化槽位，不重说一遍）。"""

    model_config = ConfigDict(extra="forbid")

    slots: list[SlotUpdate] = Field(min_length=1, max_length=18)


@router.get("/pending", summary="待确认列表（30s 超时回收的卡片）")
def list_pending(
    context: AuthContextDep,
    session: TenantSessionDep,
    limit: int = 20,
) -> JSONResponse:
    """待确认列表；复活复用同一 ``session_id``，**不会重复扣次**。"""
    ids = ids_of(context)
    rows = LedgerService(session).pending_cards(tenant_id=ids.tenant_id, limit=limit)
    return JSONResponse(
        status_code=200,
        content=ok({"items": rows, "note": "复活时不重复扣次（usage_ledger 唯一约束兜底）"}),
    )


@router.get("/{card_id}", summary="取卡片 7 区块")
def get_card(
    card_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """返回 7 区块（① 原文 ② 意图 ③ 槽位表 ④ 计算式 ⑤ 影响预览 ⑥ 风险条 ⑦ 操作区）。"""
    ids = ids_of(context)
    view = LedgerService(session).card_view(tenant_id=ids.tenant_id, card_id=card_id)
    return JSONResponse(status_code=200, content=ok(view.to_payload()))


@router.patch("/{card_id}/edit", summary="编辑槽位（本地重算，不重调 AI）")
def edit_card(
    card_id: uuid.UUID,
    payload: CardEditRequest,
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """改槽位 → 本地重算 → **重新只读预演** → 返回新卡片。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    ledger = LedgerService(session)
    card = ledger._load_card(ids.tenant_id, card_id)  # noqa: SLF001
    stored = StoredCardPayload.from_json(card.slots)

    updates = {item.code.upper(): item.value for item in payload.slots}
    runtime = runtime_for(session, settings, context)

    # 先按新值算明细与 delta（不调模型，纯结构化）
    merged_slots = {**stored.slots, **updates}
    probe = StoredCardPayload(
        intent_id=stored.intent_id,
        slots=merged_slots,
        source={**stored.source, **{code: "asr" for code in updates}},
        lines=stored.lines,
        asr_text=stored.asr_text,
        asr_confidence=stored.asr_confidence,
        calc_traces=stored.calc_traces,
    )
    spec = runtime.registry.require(stored.intent_id)
    lines = lines_of_result(
        _as_parse(probe),
        runtime=runtime,
        items=(),
        partner_id=str(merged_slots.get("C") or merged_slots.get("S") or "") or None,
    )
    deltas = build_deltas(
        intent_id=spec.intent_id,
        side_effect=dict(spec.side_effect or {}),
        lines=[line.as_dict() for line in lines if line.product_id],
    )
    preview_service = ImpactPreviewService(get_database(request))
    bundle = preview_service.preview(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        deltas=deltas,
        session=None if preview_service.dialect != "sqlite" else session,
    )

    view = ledger.edit_card(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        card_id=card_id,
        updates=updates,
        preview=bundle,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                **view.to_payload(),
                "llm_calls": 0,
                "note": "编辑只在本地重算并重新只读预演，未重新调用模型；已写 correction_event",
            }
        ),
    )


def _as_parse(payload: StoredCardPayload) -> Any:
    """把卡片载荷转成一个最小 ``ParseResult``（只为复用 ``lines_of_result``）。"""
    from app.voice.output_schema import ParseResult

    return ParseResult(
        intent=payload.intent_id,
        slots=dict(payload.slots),
        source={code: "asr" for code in payload.slots},
        confidence=payload.asr_confidence or 0.5,
        calc_traces=list(payload.calc_traces),
        route="card",
    )
