"""卡片读取与编辑：`GET /voice/cards/{id}`、`PATCH /voice/cards/{id}/edit`、`GET /voice/cards/pending`。

两条要点
-------
1. **GET 只有 7 区块 + meta**，不多不少（``ConfirmCardView.blocks()`` 的键集合是硬约束）；
2. **PATCH 不重调 AI**：改槽位 = 本地重算 + 重新只读预演，**从不重新解析原话**
   （验收⑥由 ``test_correction_no_llm.py`` 用 ``CountingLlmProvider.calls`` 钉住）。
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.v1._shared import assert_writable, ids_of, require_roles, runtime_for, WRITE_ROLES
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep, get_database
from app.models.voice import ConfirmCard
from app.services.ledger import LedgerService, StoredCardPayload
from app.services.voice_runtime import _as_decimal, _qty_base_of, _resolve, lines_of_result
from app.utils.unit import CANONICAL_UNITS
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.number_normalizer import parse_money_yuan, parse_quantity

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


class FieldVoiceRequest(BaseModel):
    """字段级语音重试（只对指定槽位做受限本地解析，0 次大模型调用）。"""

    model_config = ConfigDict(extra="forbid")

    slot_code: str = Field(min_length=1, max_length=8)
    spoken_text: str = Field(min_length=1, max_length=120)


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


@router.post("/{card_id}/revive", summary="复活超时卡片（复用同一 session，不重复扣次）")
def revive_card(
    card_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """卡片过了 TTL 但还在待处理里：重写截止时刻并重新预演，用户可以接着确认。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    row = session.execute(
        select(ConfirmCard).where(
            ConfirmCard.tenant_id == ids.tenant_id,
            ConfirmCard.card_id == card_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("E_VALIDATION", message="卡片不存在或已删除")

    creation = LedgerService(session).revive(
        tenant_id=ids.tenant_id, session_id=row.session_id
    )
    if creation is None:
        raise AppError("E_VALIDATION", message="卡片不存在或已删除")
    return JSONResponse(status_code=200, content=ok(creation.as_dict()))


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


def apply_card_updates(
    *,
    session: Any,
    request: Request,
    settings: Any,
    context: Any,
    ids: Any,
    card_id: uuid.UUID,
    updates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """统一卡片编辑与预演重算（``PATCH /edit``、``field-voice`` 与片段追述共用）。"""
    ledger = LedgerService(session)
    card = ledger._load_card(ids.tenant_id, card_id)  # noqa: SLF001
    stored = StoredCardPayload.from_json(card.slots)

    runtime = runtime_for(session, settings, context)
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

    def _recalc_qty_base(product_id: str | None, quantity: Any, unit: str) -> Any:
        matched = _resolve(runtime.products, str(merged_slots.get("P") or ""))
        if matched is not None and (not product_id or matched.product_id == product_id):
            qty_base, _ok = _qty_base_of(matched, quantity, unit)
            return qty_base
        if not product_id:
            return _as_decimal(quantity)
        entry = runtime.products.lexicon.get_by_id(product_id)
        if entry is not None:
            qty_base, _ok = _qty_base_of(entry, quantity, unit)
            return qty_base
        return _as_decimal(quantity)

    view = ledger.edit_card(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        card_id=card_id,
        updates=updates,
        preview=bundle,
        qty_base_of=_recalc_qty_base,
    )
    # 若改了 P 且命中了真实商品档案，同步回写行的 product_id / pending，保证可确认
    if "P" in updates and len(lines) == 1:
        card_row = ledger._load_card(ids.tenant_id, card_id)  # noqa: SLF001
        fresh_payload = StoredCardPayload.from_json(card_row.slots)
        if len(fresh_payload.lines) == 1:
            row0 = dict(fresh_payload.lines[0])
            resolved_line = lines[0].as_dict()
            row0["product_id"] = resolved_line.get("product_id")
            row0["product_label"] = resolved_line.get("product_label")
            row0["pending"] = bool(resolved_line.get("pending"))
            row0["qty_base"] = resolved_line.get("qty_base")
            if resolved_line.get("similar_products"):
                row0["similar_products"] = resolved_line["similar_products"]
            else:
                row0.pop("similar_products", None)
            card_row.slots = StoredCardPayload(
                intent_id=fresh_payload.intent_id,
                slots=fresh_payload.slots,
                source=fresh_payload.source,
                lines=(row0,),
                asr_text=fresh_payload.asr_text,
                asr_confidence=fresh_payload.asr_confidence,
                calc_traces=fresh_payload.calc_traces,
                expires_at=fresh_payload.expires_at,
                edited=fresh_payload.edited,
            ).to_json()
            session.flush()
            view = ledger.card_view(tenant_id=ids.tenant_id, card_id=card_id)

    return view.to_payload(), ledger.last_edit_feedback


def _parse_field_voice(
    *, slot_code: str, spoken_text: str, runtime: Any
) -> dict[str, Any]:
    """字段级受限本地解析（不调大模型）：按目标槽位提取结构化新值。"""
    code = slot_code.strip().upper()
    text = (spoken_text or "").strip()
    if not text:
        raise AppError("E_VALIDATION", message="没听清你说的新值，请再说一遍")

    if code == "Q":
        parsed = parse_quantity(text)
        if not parsed.ok or parsed.value is None:
            raise AppError("E_VALIDATION", message="没听出具体数量，可以说「五箱」或「12瓶」")
        qty: Any = (
            int(parsed.value)
            if parsed.value == parsed.value.to_integral_value()
            else float(parsed.value)
        )
        updates: dict[str, Any] = {"Q": qty}
        for unit in sorted(CANONICAL_UNITS, key=len, reverse=True):
            if unit in text:
                updates["U"] = unit
                break
        return updates

    if code in {"UP", "TA", "AMT"}:
        parsed = parse_money_yuan(text)
        if not parsed.ok or parsed.value is None:
            raise AppError("E_VALIDATION", message="没听出金额，可以说「三十五块」或「70元」")
        fen = int((parsed.value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return {code: fen}

    if code == "U":
        for unit in sorted(CANONICAL_UNITS, key=len, reverse=True):
            if unit in text:
                return {"U": unit}
        return {"U": re.sub(r"[，,。.、\s]+", "", text)[:8]}

    if code == "P":
        cleaned = re.sub(r"^(?:是|改成|换成|商品是)\s*", "", text).strip("，,。.！？ ")
        hit = runtime.products.resolve(cleaned) or runtime.products.fuzzy_match(cleaned, min_score=0.72, margin=0.05)
        if hit is not None and hit.product is not None:
            return {"P": hit.product.display_name()}
        return {"P": cleaned}

    if code in {"C", "S"}:
        cleaned = re.sub(r"^(?:是|客户是|供应商是|老板是)\s*", "", text).strip("，,。.！？ ")
        resolver = runtime.customers if code == "C" else runtime.suppliers
        hit = resolver.resolve(cleaned)
        if hit is not None and hit.product is not None:
            return {code: hit.product.name}
        return {code: cleaned}

    if code == "PS":
        if any(w in text for w in ("欠", "赊", "挂账", "未付", "下回")):
            return {"PS": "未结"}
        if "部分" in text:
            return {"PS": "部分"}
        if any(w in text for w in ("付", "结", "现金", "微信", "转")):
            return {"PS": "已结"}
        raise AppError("E_VALIDATION", message="请说「已付清」或「赊账」")

    return {code: text}


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

    updates = {item.code.upper(): item.value for item in payload.slots}
    card_payload, feedback = apply_card_updates(
        session=session,
        request=request,
        settings=settings,
        context=context,
        ids=ids,
        card_id=card_id,
        updates=updates,
    )
    resp: dict[str, Any] = {
        **card_payload,
        "llm_calls": 0,
        "note": "编辑只在本地重算并重新只读预演，未重新调用模型；已写 correction_event",
    }
    if feedback:
        resp.update(feedback)
    return JSONResponse(status_code=200, content=ok(resp))


@router.post("/{card_id}/field-voice", summary="单字段按住重说（0 次大模型调用）")
def field_voice_edit(
    card_id: uuid.UUID,
    payload: FieldVoiceRequest,
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """只对卡片上的某一格做受限语音识别回填，纯本地重算、不调大模型、不额外扣费。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    runtime = runtime_for(session, settings, context)
    updates = _parse_field_voice(
        slot_code=payload.slot_code,
        spoken_text=payload.spoken_text,
        runtime=runtime,
    )
    card_payload, feedback = apply_card_updates(
        session=session,
        request=request,
        settings=settings,
        context=context,
        ids=ids,
        card_id=card_id,
        updates=updates,
    )
    resp: dict[str, Any] = {
        **card_payload,
        "llm_calls": 0,
        "parsed_updates": updates,
        "note": "字段语音重说只在本地受限解析并重算（0 次大模型调用）",
    }
    if feedback:
        resp.update(feedback)
    return JSONResponse(status_code=200, content=ok(resp))


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
