"""确认与取消：`POST /voice/cards/{id}/confirm`、`POST /voice/cards/{id}/cancel`。

**这是写操作唯一入口**（H1）。三条不可动的顺序：
TTL → C2 二次确认（或按钮兜底）→ CAS 落账。
超时**不落账**、不自动执行、不重复扣次。
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.services.ledger import LedgerService

router = APIRouter(prefix="/voice/cards", tags=["confirm"])


class ConfirmRequest(BaseModel):
    """确认入参。"""

    model_config = ConfigDict(extra="forbid")

    confirm_type: str = Field(default="button", description="button / voice2")
    spoken_text: str | None = Field(default=None, max_length=200)
    button_fallback: bool = Field(default=False, description="C2 的按钮兜底")
    biz_date: date | None = Field(default=None, description="业务日（缺省今天）")


@router.post("/{card_id}/confirm", summary="确认落账（写操作唯一入口，CAS）")
def confirm_card(
    card_id: uuid.UUID,
    payload: ConfirmRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """确认卡片 → CAS 落账（库存/单据/应收/审计同一事务）。

    Returns:
        包络 ``data: {result, idempotency_key}``；``E_CARD_EXPIRED`` /
        ``E_PREVIEW_VERSION_CONFLICT`` 会以包络形式返回（HTTP 按契约码）。
    """
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    outcome = LedgerService(session, card_ttl_seconds=settings.card_ttl_seconds).confirm(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        card_id=card_id,
        confirm_type=payload.confirm_type,
        spoken_text=payload.spoken_text,
        button_fallback=payload.button_fallback,
        biz_date=payload.biz_date,
    )
    return JSONResponse(
        status_code=200,
        content=ok({"result": outcome.as_dict(), "idempotency_key": idempotency_key or str(card_id)}),
    )


@router.post("/{card_id}/cancel", summary="取消卡片（不写库、不再扣次）")
def cancel_card(
    card_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """取消卡片：**不写业务库**；卡片生成时那一笔不退回（已发生）。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    cancelled = LedgerService(session).cancel(
        tenant_id=ids.tenant_id, member_id=ids.member_id, card_id=card_id
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "card_id": cancelled,
                "status": "cancelled",
                "credit_refunded": False,
                "note": "取消不写业务库；卡片生成时扣的那 1 次不退回（解析成本已发生）",
            }
        ),
    )
