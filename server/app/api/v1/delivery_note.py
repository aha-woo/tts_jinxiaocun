"""拍进货单 API（M2）：拍照识别 + 核对确认入库 + 历史原件列表。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import ids_of
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, DatabaseDep, SettingsDep, TenantSessionDep
from app.services.delivery_note import confirm_delivery_note, list_delivery_notes, parse_delivery_note

router = APIRouter(prefix="/delivery-note", tags=["delivery-note"])


class DeliveryNoteParseRequest(BaseModel):
    """拍进货单识别请求（支持 base64 / URL / 对象存储引用 / 测试提示）。"""

    model_config = ConfigDict(extra="forbid")

    image_base64: str | None = Field(default=None, max_length=100_000)
    image_url: str | None = Field(default=None, max_length=2000)
    object_ref: str | None = Field(default=None, max_length=1000)
    hint_text: str | None = Field(default=None, max_length=4000)


class DeliveryNoteLineInput(BaseModel):
    """商户核对后的单行明细（允许携带解析展示辅助字段）。"""

    model_config = ConfigDict(extra="ignore")

    product_name: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=16)
    quantity: float = Field(gt=0)
    unit_price_fen: int | None = Field(default=None, ge=0)
    amount_fen: int | None = Field(default=None, ge=0)
    product_id: str | None = None
    matched_product_id: str | None = None
    unit_ratio: str | float | int | None = None


class DeliveryNoteConfirmRequest(BaseModel):
    """核对后确认入库。"""

    model_config = ConfigDict(extra="forbid")

    supplier_name: str | None = Field(default=None, max_length=120)
    doc_date: str | None = Field(default=None, max_length=16)
    payment_status: str = Field(default="已结", max_length=16)
    stated_total_fen: int | None = Field(default=None, ge=0)
    lines: list[DeliveryNoteLineInput] = Field(min_length=1, max_length=50)


@router.post("/parse", summary="拍照识别进货单")
def api_parse_delivery_note(
    payload: DeliveryNoteParseRequest,
    context: AuthContextDep,
    settings: SettingsDep,
    database: DatabaseDep,
) -> JSONResponse:
    """调用中国开源多模态视觉模型提取进货单明细并保存草稿附件。"""
    ids = ids_of(context)
    data = parse_delivery_note(
        database=database,
        settings=settings,
        tenant_context=context,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        image_base64=payload.image_base64,
        image_url=payload.image_url,
        object_ref=payload.object_ref,
        hint_text=payload.hint_text,
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.post("/{attachment_id}/confirm", summary="核对后确认进货单入库")
def api_confirm_delivery_note(
    attachment_id: str,
    payload: DeliveryNoteConfirmRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    database: DatabaseDep,
) -> JSONResponse:
    """通过 LedgerService 统一入账，原单标记 source='photo' 并绑定凭证附件。"""
    try:
        att_uuid = uuid.UUID(attachment_id)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="attachment_id 格式不合法") from exc

    ids = ids_of(context)
    data = confirm_delivery_note(
        session=session,
        database=database,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        attachment_id=att_uuid,
        lines=[line.model_dump() for line in payload.lines],
        supplier_name=payload.supplier_name,
        doc_date=payload.doc_date,
        payment_status=payload.payment_status,
        stated_total_fen=payload.stated_total_fen,
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.get("/attachments", summary="进货单照片列表")
def api_list_delivery_notes(
    context: AuthContextDep,
    session: TenantSessionDep,
    limit: int = 20,
) -> JSONResponse:
    """查看最近拍过的进货单与关联入账状态。"""
    ids = ids_of(context)
    items = list_delivery_notes(
        session, tenant_id=ids.tenant_id, store_id=ids.store_id, limit=limit
    )
    return JSONResponse(status_code=200, content=ok({"items": items}))
