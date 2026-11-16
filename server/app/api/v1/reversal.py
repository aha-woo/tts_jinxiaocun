"""撤销：`POST /docs/{id}/reversal` —— 反做单（原单保留），10 分钟窗口（可配）。

被引用就拒（``E_DOC_REFERENCED``）并返回引用链，让用户知道**为什么**不能撤。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.services.reversal import ReversalService

router = APIRouter(prefix="/docs", tags=["reversal"])


class ReversalRequest(BaseModel):
    """撤销入参。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=200)


@router.post("/{doc_id}/reversal", summary="撤销 = 反做单（原单不删）")
def reverse_doc(
    doc_id: uuid.UUID,
    payload: ReversalRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """生成反做单；超出窗口 ``E_REVERSAL_WINDOW_CLOSED``，被引用 ``E_DOC_REFERENCED``。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    outcome = ReversalService(session).reverse(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        doc_id=doc_id,
        member_id=ids.member_id,
        reason=payload.reason,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "result": outcome.as_dict(),
                "window_minutes": settings.reversal_window_minutes,
                "idempotency_key": idempotency_key or f"{doc_id}:{ids.member_id}",
            }
        ),
    )


@router.get("/{doc_id}/references", summary="查看引用链（撤销前预检）")
def list_references(
    doc_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """列出引用这张单的对象（撤销前先看，避免白点一次）。"""
    ids = ids_of(context)
    links = ReversalService(session).references_of(tenant_id=ids.tenant_id, doc_id=doc_id)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "doc_id": str(doc_id),
                "references": [link.as_dict() for link in links],
                "blocked": bool(links),
            }
        ),
    )
