"""商品档案：缺进价清单与补价。

为什么单独做
------------
商品档案是进销存的基础数据：没有进价，毛利永远是错的。缺进价不该只藏在货品页的
筛选里，要能像待办一样被看到、被补上。

说明：供应商是**单据级**信息（同一件货可以不同供应商），不存在"商品档案缺供应商"，
所以这里只补价格，不虚构供应商字段。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from app.api.v1._shared import WRITE_ROLES, assert_writable, can_view_cost, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.models.catalog import Product
from app.models.ops import Gap
from app.services.gaps import resolve_gaps
from app.services.product_merge import merge_products
from app.services.unit_upgrade import set_unit_ratio_and_maybe_upgrade
from app.utils.date_utils import now_utc

router = APIRouter(prefix="/products", tags=["products"])

_ALLOWED_METHODS = ("", "avg", "batch", "fifo", "fefe", "fefo")


class ProductPatch(BaseModel):
    """补价格、成本法、商品级批次开关与负库存覆盖。"""

    model_config = ConfigDict(extra="forbid")

    last_purchase_price_fen: int | None = Field(default=None, ge=0)
    last_sale_price_fen: int | None = Field(default=None, ge=0)
    cost_method: str | None = Field(default=None, max_length=8)
    track_batch: bool | None = None
    allow_negative: bool | None = None
    low_stock_qty: Decimal | None = Field(default=None, ge=0)


class UnitRatioRequest(BaseModel):
    """设置商品单位换算（若原基本单位拆成更细单位则自动原子升级账目）。"""

    model_config = ConfigDict(extra="forbid")

    from_unit: str = Field(min_length=1, max_length=16)
    to_unit: str = Field(min_length=1, max_length=16)
    ratio: Decimal = Field(gt=0)


def _as_dict(product: Product, *, show_cost: bool = True) -> dict[str, object]:
    return {
        "product_id": str(product.product_id),
        "name": product.name,
        "spec": product.spec,
        "base_unit": product.base_unit,
        "last_purchase_price_fen": (
            int(product.last_purchase_price_fen)
            if (show_cost and product.last_purchase_price_fen is not None)
            else None
        ),
        "last_sale_price_fen": (
            int(product.last_sale_price_fen)
            if product.last_sale_price_fen is not None
            else None
        ),
        "avg_cost_fen": (
            int(product.avg_cost_fen)
            if (show_cost and product.avg_cost_fen is not None)
            else None
        ),
        "track_batch": bool(product.track_batch),
        "allow_negative": product.allow_negative,
        "low_stock_qty": str(product.low_stock_qty) if product.low_stock_qty is not None else None,
        "cost_method": product.cost_method,
        "cost_hidden": not show_cost,
        "status": product.status,
    }


@router.get("/gaps", summary="商品档案待补（缺进价；含 draft 与 active）")
def product_gaps(
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """列出缺进价（NULL 或 0）的商品：补上之前，毛利不算它们。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    rows = list(
        session.execute(
            select(Product)
            .where(
                Product.tenant_id == ids.tenant_id,
                Product.status.in_(("draft", "active")),
                or_(
                    Product.last_purchase_price_fen.is_(None),
                    Product.last_purchase_price_fen == 0,
                ),
            )
            .order_by(Product.created_at.desc())
            .limit(100)
        ).scalars()
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [_as_dict(row, show_cost=show_cost) for row in rows],
                "readonly": True,
                "kind": "cost",
                "cost_hidden": not show_cost,
            }
        ),
    )


@router.get("/unified-gaps", summary="统一待处理队列（负库存 / 缺进价 / 缺客户 / 未确认卡等）")
def list_unified_gaps(
    context: AuthContextDep,
    session: TenantSessionDep,
    status: str = "open",
    limit: int = 100,
) -> JSONResponse:
    """返回统一 ``gap`` 待处理条目，按优先级和创建时间排序。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    stmt = select(Gap).where(Gap.tenant_id == ids.tenant_id, Gap.store_id == ids.store_id)
    if status:
        stmt = stmt.where(Gap.status == status)
    if not show_cost:
        stmt = stmt.where(Gap.kind != "missing_cost")
    rows = list(
        session.execute(
            stmt.order_by(Gap.priority.asc(), Gap.created_at.desc()).limit(min(limit, 200))
        ).scalars()
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [
                    {
                        "gap_id": str(row.gap_id),
                        "kind": str(row.kind),
                        "ref_entity": str(row.ref_entity),
                        "ref_id": str(row.ref_id),
                        "title": str(row.title),
                        "detail_json": row.detail_json,
                        "priority": int(row.priority),
                        "status": str(row.status),
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ],
                "readonly": True,
            }
        ),
    )


@router.post("/unified-gaps/{gap_id}/resolve", summary="标记待处理已解决")
def resolve_unified_gap(
    gap_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    row = session.execute(
        select(Gap).where(Gap.tenant_id == ids.tenant_id, Gap.gap_id == gap_id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise AppError("E_VALIDATION", message="待处理条目不存在")
    row.status = "resolved"
    row.resolved_at = now_utc()
    session.flush()
    return JSONResponse(status_code=200, content=ok({"gap_id": str(row.gap_id), "status": "resolved"}))


@router.patch("/{product_id}", summary="补商品价格 / 批次开关 / 负库存覆盖")
def patch_product(
    product_id: uuid.UUID,
    payload: ProductPatch,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """更新商品价格、批次跟踪开关、负库存覆盖与低库存阈值；补齐双价后草稿自动转正。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    product = session.execute(
        select(Product).where(
            Product.tenant_id == ids.tenant_id, Product.product_id == product_id
        )
    ).scalar_one_or_none()
    if product is None:
        raise AppError("E_VALIDATION", message="商品不存在或已删除")
    fields = payload.model_fields_set
    if not fields:
        raise AppError("E_VALIDATION", message="没有要改的内容")
    if payload.cost_method is not None and payload.cost_method not in _ALLOWED_METHODS:
        raise AppError("E_VALIDATION", message="成本法只能是 avg / batch")

    if "last_purchase_price_fen" in fields and payload.last_purchase_price_fen is not None:
        product.last_purchase_price_fen = int(payload.last_purchase_price_fen)
        if product.avg_cost_fen is None:
            product.avg_cost_fen = int(payload.last_purchase_price_fen)
        resolve_gaps(
            session,
            tenant_id=ids.tenant_id,
            store_id=ids.store_id,
            kind="missing_cost",
            ref_entity="product",
            ref_id=product.product_id,
        )
    if "last_sale_price_fen" in fields and payload.last_sale_price_fen is not None:
        product.last_sale_price_fen = int(payload.last_sale_price_fen)
    if "cost_method" in fields:
        product.cost_method = payload.cost_method or None
    if "track_batch" in fields and payload.track_batch is not None:
        product.track_batch = bool(payload.track_batch)
        if product.track_batch and not product.cost_method:
            product.cost_method = "batch"
        elif not product.track_batch and product.cost_method in ("batch", "fifo", "fefe", "fefo"):
            product.cost_method = "avg"
    if "allow_negative" in fields:
        product.allow_negative = payload.allow_negative
    if "low_stock_qty" in fields:
        product.low_stock_qty = payload.low_stock_qty

    if (
        str(product.status) == "draft"
        and product.last_purchase_price_fen is not None
        and product.last_sale_price_fen is not None
    ):
        product.status = "active"
    session.flush()
    show_cost = can_view_cost(session, context)
    return JSONResponse(
        status_code=200,
        content=ok({"product": _as_dict(product, show_cost=show_cost)}),
    )


@router.post("/{product_id}/unit-ratio", summary="设置单位换算系数（必要时原子升级基本单位）")
def set_unit_ratio(
    product_id: uuid.UUID,
    payload: UnitRatioRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """设置 ``1 from_unit = ratio × to_unit``，将旧基本单位历史库存与单据同事务折算升级。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    outcome = set_unit_ratio_and_maybe_upgrade(
        session,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        product_id=product_id,
        from_unit=payload.from_unit,
        to_unit=payload.to_unit,
        ratio=payload.ratio,
    )
    return JSONResponse(status_code=200, content=ok({"unit_ratio": outcome.as_dict()}))


class MergeProductRequest(BaseModel):
    """事后合并重复商品入参。"""

    model_config = ConfigDict(extra="forbid")

    target_product_id: uuid.UUID


@router.post("/{product_id}/merge", summary="合并重复商品（迁移库存、流水、单据行与别名）")
def api_merge_product(
    product_id: uuid.UUID,
    payload: MergeProductRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """将重复商品 ``product_id`` 合并入 ``target_product_id``，保留源商品别名为目标别名。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)
    outcome = merge_products(
        session,
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        source_product_id=product_id,
        target_product_id=payload.target_product_id,
    )
    return JSONResponse(status_code=200, content=ok({"merge": outcome.as_dict()}))
