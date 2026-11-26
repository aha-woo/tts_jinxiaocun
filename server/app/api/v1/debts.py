"""欠款核销与客户/供应商对账单接口（M2/V1 补齐）。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import WRITE_ROLES, assert_writable, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, DatabaseDep, TenantSessionDep
from app.services.debts import partner_statement, settle_partner_debt

router = APIRouter(prefix="/debts", tags=["debts"])


class DebtSettleRequest(BaseModel):
    """登记收款/付款核销请求。"""

    model_config = ConfigDict(extra="forbid")

    partner_id: str = Field(min_length=1, max_length=64)
    direction: Literal["receivable", "payable"]
    amount_fen: int = Field(gt=0)
    method: str = Field(default="微信", max_length=32)


@router.post("/settle", summary="欠款页直接登记收/付款核销")
def api_settle_debt(
    payload: DebtSettleRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    database: DatabaseDep,
) -> JSONResponse:
    """经由 LedgerService 统一完成核销入账，绝不跳过流水与审计。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    try:
        pid = uuid.UUID(payload.partner_id)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="partner_id 格式不合法") from exc

    data = settle_partner_debt(
        session,
        database,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        partner_id=pid,
        direction=payload.direction,
        amount_fen=payload.amount_fen,
        method=payload.method,
    )
    return JSONResponse(status_code=200, content=ok(data))


@router.get("/{partner_id}/statement", summary="生成客户/供应商对账单详情")
def api_partner_statement(
    partner_id: str,
    context: AuthContextDep,
    session: TenantSessionDep,
    direction: Literal["receivable", "payable"] | None = None,
) -> JSONResponse:
    """返回门店名、往来单位、未结单据明细与总欠款，供端侧绘成对账单图片发微信。"""
    ids = ids_of(context)
    try:
        pid = uuid.UUID(partner_id)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="partner_id 格式不合法") from exc

    data = partner_statement(
        session,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        partner_id=pid,
        direction=direction,
    )
    return JSONResponse(status_code=200, content=ok(data))
