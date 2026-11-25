"""盘点接口：开盘点 / 查看 / 填实盘 / 应用差异。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.v1._shared import WRITE_ROLES, assert_writable, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.models.catalog import Product
from app.models.inventory import CountLine, CountSession
from app.services import counts as count_service

router = APIRouter(prefix="/counts", tags=["counts"])


class FillRequest(BaseModel):
    """填实盘数。"""

    model_config = ConfigDict(extra="forbid")

    qty_counted: str = Field(min_length=1, max_length=24)
    reason: str | None = Field(default=None, max_length=24)


@router.post("", summary="开盘点（快照当前库存）")
def start_count(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """打开一次盘点：把当前库存快照成账面数，之后填实盘、应用差异。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    count = count_service.create_count(
        session, tenant_id=ids.tenant_id, store_id=ids.store_id, member_id=ids.member_id
    )
    return JSONResponse(
        status_code=200,
        content=ok({"count_id": str(count.count_id), "status": count.status}),
    )


@router.get("/{count_id}", summary="盘点明细（含差异）")
def get_count(
    count_id: uuid.UUID, context: AuthContextDep, session: TenantSessionDep
) -> JSONResponse:
    """返回账面数、实盘数、差异；没填的行 qty_counted 为空。"""
    ids = ids_of(context)
    count = session.execute(
        select(CountSession).where(
            CountSession.tenant_id == ids.tenant_id, CountSession.count_id == count_id
        )
    ).scalar_one_or_none()
    if count is None:
        raise AppError("E_VALIDATION", message="盘点单不存在")
    lines = list(
        session.execute(
            select(CountLine).where(
                CountLine.tenant_id == ids.tenant_id, CountLine.count_id == count_id
            )
        ).scalars()
    )
    products = {
        row.product_id: row
        for row in session.execute(
            select(Product).where(
                Product.tenant_id == ids.tenant_id,
                Product.product_id.in_([line.product_id for line in lines] or [uuid.uuid4()]),
            )
        ).scalars()
    }
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "count": {
                    "count_id": str(count.count_id),
                    "status": str(count.status),
                    "started_at": count.started_at.isoformat(),
                    "applied_at": count.applied_at.isoformat() if count.applied_at else None,
                },
                "lines": [
                    {
                        "product_id": str(line.product_id),
                        "name": products[line.product_id].name
                        if line.product_id in products
                        else "",
                        "unit": products[line.product_id].base_unit
                        if line.product_id in products
                        else "",
                        "qty_book": str(line.qty_book),
                        "qty_counted": str(line.qty_counted) if line.qty_counted is not None else None,
                        "diff": str(line.qty_counted - line.qty_book)
                        if line.qty_counted is not None
                        else None,
                        "reason": line.reason,
                    }
                    for line in lines
                ],
            }
        ),
    )


@router.patch("/{count_id}/lines/{product_id}", summary="填一行实盘数")
def fill_count_line(
    count_id: uuid.UUID,
    product_id: uuid.UUID,
    payload: FillRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """只写实盘数，不动物流；应用差异走 apply。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    line = count_service.fill_line(
        session,
        tenant_id=ids.tenant_id,
        count_id=count_id,
        product_id=product_id,
        qty_counted=payload.qty_counted,
        reason=payload.reason,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "product_id": str(line.product_id),
                "qty_book": str(line.qty_book),
                "qty_counted": str(line.qty_counted),
            }
        ),
    )


@router.post("/{count_id}/apply", summary="应用盘点（写 count_adjust 流水）")
def apply_count(
    count_id: uuid.UUID, context: AuthContextDep, session: TenantSessionDep
) -> JSONResponse:
    """差异写流水、调余额与批次；盘点单变 applied，不可重复应用。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    result = count_service.apply_count(
        session,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        count_id=count_id,
    )
    return JSONResponse(status_code=200, content=ok({"result": result.as_dict()}))
