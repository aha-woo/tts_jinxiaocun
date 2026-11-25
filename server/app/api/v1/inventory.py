"""库存策略、批次与对账接口。

成本法收敛为 ``avg``（移动平均，默认）与 ``batch``（按批次实际成本，兼容历史 ``fifo``/``fefe``/``fefo``）；
负库存默认允许，仅老板可改全局策略与店员成本可见性开关。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.v1._shared import BOSS_ONLY, assert_writable, can_view_cost, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.models.catalog import Product
from app.models.inventory import InventoryPolicy, StockBatch
from app.models.tenant import Tenant
from app.services import batch as batch_service

router = APIRouter(prefix="/inventory", tags=["inventory"])

_METHODS = {"avg", "batch", "fifo", "fefe", "fefo"}


class PolicyPatch(BaseModel):
    """库存策略入参。"""

    model_config = ConfigDict(extra="forbid")

    cost_method: str | None = None
    allow_negative: bool | None = None
    staff_can_view_cost: bool | None = None


@router.get("/policy", summary="库存策略（成本法 / 负库存 / 成本可见性）")
def get_policy(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """没配置过时返回默认 ``avg`` + ``allow_negative=True``。"""
    ids = ids_of(context)
    row = session.execute(
        select(InventoryPolicy).where(InventoryPolicy.tenant_id == ids.tenant_id).limit(1)
    ).scalar_one_or_none()
    tenant = session.get(Tenant, ids.tenant_id)
    default_neg = bool(tenant.allow_negative) if tenant is not None else True
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "cost_method": str(row.cost_method) if row else "avg",
                "allow_negative": bool(row.allow_negative) if row else default_neg,
                "staff_can_view_cost": bool(tenant.staff_can_view_cost) if tenant else False,
            }
        ),
    )


@router.patch("/policy", summary="改库存策略（仅老板；只影响之后出库）")
def patch_policy(
    payload: PolicyPatch,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """只影响之后的出库，不重算历史；仅老板可改。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    if payload.cost_method is not None and payload.cost_method not in _METHODS:
        raise AppError("E_VALIDATION", message="成本法只能是 avg（移动平均）或 batch（批次实际成本）")
    tenant = session.get(Tenant, ids.tenant_id)
    row = session.execute(
        select(InventoryPolicy).where(InventoryPolicy.tenant_id == ids.tenant_id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        default_neg = bool(tenant.allow_negative) if tenant is not None else True
        row = InventoryPolicy(
            tenant_id=ids.tenant_id,
            cost_method=payload.cost_method or "avg",
            allow_negative=default_neg if payload.allow_negative is None else bool(payload.allow_negative),
        )
        session.add(row)
    else:
        if payload.cost_method is not None:
            row.cost_method = payload.cost_method
        if payload.allow_negative is not None:
            row.allow_negative = payload.allow_negative
    if tenant is not None:
        if payload.allow_negative is not None:
            tenant.allow_negative = bool(payload.allow_negative)
        if payload.staff_can_view_cost is not None:
            tenant.staff_can_view_cost = bool(payload.staff_can_view_cost)
    session.flush()
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "cost_method": str(row.cost_method),
                "allow_negative": bool(row.allow_negative),
                "staff_can_view_cost": bool(tenant.staff_can_view_cost) if tenant else False,
            }
        ),
    )


@router.get("/batches", summary="批次余量（含商品名/批次标签/虚拟批；按权限控制成本）")
def list_batches(
    context: AuthContextDep,
    session: TenantSessionDep,
    product_id: uuid.UUID | None = None,
    status: str = "open",
    limit: int = 100,
) -> JSONResponse:
    """只读：批次余量、标签、虚拟批状态与到期日。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    statement = (
        select(StockBatch, Product)
        .outerjoin(
            Product,
            (Product.product_id == StockBatch.product_id) & (Product.tenant_id == StockBatch.tenant_id),
        )
        .where(StockBatch.tenant_id == ids.tenant_id, StockBatch.store_id == ids.store_id)
    )
    if product_id is not None:
        statement = statement.where(StockBatch.product_id == product_id)
    if status != "":
        statement = statement.where(StockBatch.status == status)
    rows = list(
        session.execute(
            statement.order_by(StockBatch.is_virtual.desc(), StockBatch.received_at, StockBatch.batch_id).limit(
                min(limit, 200)
            )
        ).all()
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [
                    {
                        "batch_id": str(batch.batch_id),
                        "product_id": str(batch.product_id),
                        "product_name": str(prod.name) if prod is not None else "",
                        "base_unit": str(prod.base_unit) if prod is not None else "",
                        "label": str(batch.label or ("未到货" if batch.is_virtual else "")),
                        "is_virtual": bool(batch.is_virtual),
                        "qty_in": str(batch.qty_in),
                        "qty_left": str(batch.qty_left),
                        "unit_cost_fen": (
                            int(batch.unit_cost_fen)
                            if (show_cost and batch.unit_cost_fen is not None)
                            else None
                        ),
                        "cost_hidden": not show_cost,
                        "expiry_date": str(batch.expiry_date) if batch.expiry_date else None,
                        "status": batch.status,
                        "received_at": batch.received_at.isoformat(),
                    }
                    for batch, prod in rows
                ],
                "cost_hidden": not show_cost,
                "readonly": True,
            }
        ),
    )


@router.get("/reconcile", summary="库存对账校验（仅老板，只读）")
def reconcile_inventory(
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """校验库存余额 == 流水合计、批次余量 == 库存余额、未结清暂估流水。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    report = batch_service.reconcile_store(
        session, tenant_id=ids.tenant_id, store_id=ids.store_id
    )
    return JSONResponse(status_code=200, content=ok(report))
