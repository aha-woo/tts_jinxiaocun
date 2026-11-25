"""撤销与一步原子改单：`POST /docs/{id}/reversal`、`POST /docs/{id}/correct`。

权限边界：
- 老板/店长（boss / manager）：可撤可改本店任意未引用历史单（不设 10 分钟硬窗）；
- 店员（clerk / staff）：只能撤/改自己**今天**记的单，历史单或他人单拒绝。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import WRITE_ROLES, assert_writable, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.domain.doc_service import DocService
from app.services.reversal import ReversalService
from app.utils.date_utils import today_in_tz

router = APIRouter(prefix="/docs", tags=["reversal"])


class ReversalRequest(BaseModel):
    """撤销入参。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=200)


class CorrectLineInput(BaseModel):
    """一步改单明细行入参。"""

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    unit: str | None = Field(default=None, max_length=16)
    qty_base: Decimal | None = Field(default=None, gt=0)
    unit_price_fen: int | None = Field(default=None, ge=0)
    amount_fen: int | None = Field(default=None, ge=0)


class CorrectDocRequest(BaseModel):
    """一步改单入参。"""

    model_config = ConfigDict(extra="forbid")

    lines: list[CorrectLineInput] = Field(min_length=1)
    reason: str = Field(default="", max_length=200)
    biz_date: date | None = None
    payment_status: str | None = Field(default=None, max_length=16)


def _enforce_doc_permission(
    session: Any, *, tenant_id: uuid.UUID, doc_id: uuid.UUID, member_id: uuid.UUID, role: str
) -> bool:
    """校验改/撤权限；返回是否允许忽略时间窗（老板/店长 True）。"""
    role_norm = (role or "").strip().lower()
    if role_norm in ("boss", "manager"):
        return True
    doc = DocService(session).get(tenant_id=tenant_id, doc_id=doc_id)
    if doc is None:
        raise AppError("E_VALIDATION", message="单据不存在或不属于当前租户")
    if doc.created_by != member_id or doc.biz_date != date.fromisoformat(today_in_tz("Asia/Shanghai")):
        raise AppError(
            "E_ROLE_DENIED",
            message="店员只能撤改自己今天记的单，历史单或他人单请联系老板",
        )
    return False


@router.post("/{doc_id}/reversal", summary="撤销 = 反做单（原单不删）")
def reverse_doc(
    doc_id: uuid.UUID,
    payload: ReversalRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """生成反做单；老板可撤历史单，被引用 ``E_DOC_REFERENCED``。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    ignore_window = _enforce_doc_permission(
        session,
        tenant_id=ids.tenant_id,
        doc_id=doc_id,
        member_id=ids.member_id,
        role=context.role_code,
    )

    outcome = ReversalService(session).reverse(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        doc_id=doc_id,
        member_id=ids.member_id,
        reason=payload.reason,
        ignore_window=ignore_window,
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


@router.post("/{doc_id}/correct", summary="一步原子改单（同事务反做原单 + 新建更正单）")
def correct_doc(
    doc_id: uuid.UUID,
    payload: CorrectDocRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """一步完成改单，返回原单/反做单/新更正单的关联结果。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    _enforce_doc_permission(
        session,
        tenant_id=ids.tenant_id,
        doc_id=doc_id,
        member_id=ids.member_id,
        role=context.role_code,
    )

    outcome = ReversalService(session).correct_doc(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        doc_id=doc_id,
        member_id=ids.member_id,
        lines=[line.model_dump() for line in payload.lines],
        reason=payload.reason,
        biz_date=payload.biz_date,
        payment_status=payload.payment_status,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "result": outcome.as_dict(),
                "idempotency_key": idempotency_key or f"correct:{doc_id}:{ids.member_id}",
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
