"""只读查询：`GET /query/{stock,docs,debt,report}` —— **R0 不写库、不占卡片、不扣次**。

这些端点存在的意义就是「不给确认卡片」：查个库存还要点确认，用户会疯。
所以它们直接返回结果，并且**绝不**碰 ``usage_ledger`` 或 ``confirm_card``。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1._shared import can_view_cost, ids_of
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.domain.doc_service import DocService
from app.domain.report_service import ReportService
from app.models.catalog import Product
from app.services.ledger import LedgerService
from app.utils.date_utils import today_in_tz
from app.utils.money import format_fen

router = APIRouter(prefix="/query", tags=["query"])


@router.get("/stock", summary="查库存（R0）")
def query_stock(
    context: AuthContextDep,
    session: TenantSessionDep,
    name: str | None = None,
    low_only: bool = False,
    limit: int = 20,
) -> JSONResponse:
    """按商品名模糊查库存；``low_only`` 只看低库存；按角色裁剪进价与成本。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    safe_limit = min(max(1, limit), 500)
    service = ReportService(session)
    rows = (
        service.low_stock(tenant_id=ids.tenant_id, store_id=ids.store_id, limit=safe_limit)
        if low_only
        else service.stock_of(
            tenant_id=ids.tenant_id, store_id=ids.store_id, product_name=name, limit=safe_limit
        )
    )
    items: list[dict[str, object]] = []
    for row in rows:
        item = row.as_dict()
        if not show_cost:
            item["avg_cost_fen"] = None
            item["last_purchase_price_fen"] = None
            item["cost_hidden"] = True
        else:
            item["cost_hidden"] = False
        items.append(item)
    return JSONResponse(
        status_code=200,
        content=ok({"items": items, "cost_hidden": not show_cost, "readonly": True, "billed": False}),
    )


@router.get("/docs/candidates", summary="改单候选（R0，只读）")
def query_doc_candidates(
    context: AuthContextDep,
    session: TenantSessionDep,
    ref: str = "",
    limit: int = 3,
) -> JSONResponse:
    """说「刚才那笔」时列候选，让老板点选，避免改错单。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    rows = LedgerService(session).reversal_candidates(
        tenant_id=ids.tenant_id, raw_ref=ref, limit=limit
    )
    items = []
    for row in rows:
        hide = (not show_cost) and (
            str(row.doc_type) in ("purchase", "purchase_return")
            or str(row.intent_id).startswith("purchase")
        )
        items.append(
            {
                "doc_id": str(row.doc_id),
                "doc_no": str(row.doc_no),
                "intent_id": str(row.intent_id),
                "biz_date": str(row.biz_date),
                "total_amount_fen": None if hide else int(row.total_amount_fen),
                "total_amount_display": "—" if hide else format_fen(int(row.total_amount_fen)),
                "cost_hidden": hide,
            }
        )
    return JSONResponse(
        status_code=200,
        content=ok({"items": items, "ambiguous": len(items) > 1, "readonly": True}),
    )


@router.get("/docs/{doc_id}", summary="单据详情（R0，含商品名与明细行）")
def query_doc_detail(
    doc_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """单张单据的摘要 + 明细行：给「一步改单卡」显示改前值，按角色脱敏进价与毛利。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    service = DocService(session)
    header = service.get(tenant_id=ids.tenant_id, doc_id=doc_id)
    if header is None:
        raise AppError("E_VALIDATION", message="单据不存在或已删除")
    lines = service.lines_of(tenant_id=ids.tenant_id, doc_id=doc_id)
    product_ids = [line.product_id for line in lines]
    prod_names: dict[uuid.UUID, str] = {}
    if product_ids:
        for p in session.execute(
            select(Product).where(Product.tenant_id == ids.tenant_id, Product.product_id.in_(product_ids))
        ).scalars():
            prod_names[p.product_id] = str(p.name)

    hide_purchase_money = (not show_cost) and str(header.doc_type) in ("purchase", "purchase_return")
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "doc": {
                    "doc_id": str(header.doc_id),
                    "doc_no": str(header.doc_no),
                    "doc_type": str(header.doc_type),
                    "intent_id": str(header.intent_id),
                    "biz_date": str(header.biz_date),
                    "payment_status": str(header.payment_status),
                    "status": str(header.status),
                    "total_amount_fen": None if hide_purchase_money else int(header.total_amount_fen),
                    "total_amount_display": (
                        "成本已隐藏"
                        if hide_purchase_money
                        else format_fen(int(header.total_amount_fen))
                    ),
                    "cogs_fen": int(header.cogs_fen) if (show_cost and header.cogs_fen is not None) else None,
                    "cogs_display": (
                        format_fen(int(header.cogs_fen))
                        if (show_cost and header.cogs_fen is not None)
                        else None
                    ),
                    "cost_hidden": not show_cost,
                },
                "lines": [
                    {
                        "line_id": str(line.line_id),
                        "product_id": str(line.product_id),
                        "product_name": prod_names.get(line.product_id, ""),
                        "qty": str(line.qty),
                        "unit": str(line.unit),
                        "unit_price_fen": (
                            None
                            if (hide_purchase_money or line.unit_price_fen is None)
                            else int(line.unit_price_fen)
                        ),
                        "amount_fen": (
                            None
                            if (hide_purchase_money or line.amount_fen is None)
                            else int(line.amount_fen)
                        ),
                        "amount_display": (
                            "成本已隐藏"
                            if hide_purchase_money
                            else (format_fen(int(line.amount_fen)) if line.amount_fen is not None else "待补")
                        ),
                    }
                    for line in lines
                ],
                "cost_hidden": not show_cost,
                "readonly": True,
                "billed": False,
            }
        ),
    )


@router.get("/docs", summary="查单据（R0）")
def query_docs(
    context: AuthContextDep,
    session: TenantSessionDep,
    biz_date: date | None = None,
    doc_type: str | None = None,
    limit: int = 20,
) -> JSONResponse:
    """按日期/类型查单据列表（无权限时隐藏进货金额）。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    rows = ReportService(session).docs(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        biz_date=biz_date,
        doc_type=doc_type,
        limit=limit,
    )
    items: list[dict[str, object]] = []
    for row in rows:
        item = row.as_dict()
        if not show_cost and row.doc_type in ("purchase", "purchase_return"):
            item["total_amount_fen"] = None
            item["total_amount_display"] = "成本已隐藏"
            item["cost_hidden"] = True
        else:
            item["cost_hidden"] = False
        items.append(item)
    return JSONResponse(
        status_code=200,
        content=ok({"items": items, "cost_hidden": not show_cost, "readonly": True, "billed": False}),
    )


@router.get("/debt", summary="查欠款（R0）")
def query_debt(
    context: AuthContextDep,
    session: TenantSessionDep,
    partner_id: uuid.UUID | None = None,
    direction: str | None = None,
    limit: int = 50,
) -> JSONResponse:
    """欠款汇总（未结才计入）。"""
    ids = ids_of(context)
    rows = ReportService(session).debts(
        tenant_id=ids.tenant_id, partner_id=partner_id, direction=direction, limit=limit
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [row.as_dict() for row in rows],
                "total_outstanding_fen": sum(row.outstanding_fen for row in rows),
                "readonly": True,
                "billed": False,
            }
        ),
    )


@router.get("/report", summary="查报表（R0）")
def query_report(
    context: AuthContextDep,
    session: TenantSessionDep,
    biz_date: date | None = None,
    period: Literal["today", "week", "month"] = "today",
    top: bool = False,
) -> JSONResponse:
    """经营快报 / 销量 Top N（支持 today/week/month，中国时区，按角色控制进价/毛利可见性）。"""
    ids = ids_of(context)
    show_cost = can_view_cost(session, context)
    service = ReportService(session)
    if top:
        return JSONResponse(
            status_code=200,
            content=ok(
                {
                    "top_products": list(
                        service.top_products(tenant_id=ids.tenant_id, store_id=ids.store_id)
                    ),
                    "readonly": True,
                    "billed": False,
                }
            ),
        )
    anchor = biz_date or date.fromisoformat(today_in_tz("Asia/Shanghai"))
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        payload = service.period(
            tenant_id=ids.tenant_id, store_id=ids.store_id, start=start, end=anchor
        )
        payload["period"] = "week"
    elif period == "month":
        start = anchor.replace(day=1)
        payload = service.period(
            tenant_id=ids.tenant_id, store_id=ids.store_id, start=start, end=anchor
        )
        payload["period"] = "month"
    else:
        report = service.today(
            tenant_id=ids.tenant_id,
            store_id=ids.store_id,
            biz_date=anchor,
        )
        payload = report.as_dict()
        payload["period"] = "today"
    if not show_cost:
        payload["purchase_fen"] = None
        payload["purchase_display"] = "成本已隐藏"
        payload["gross_profit_fen"] = None
        payload["gross_profit_display"] = None
        payload["cost_hidden"] = True
    else:
        payload["cost_hidden"] = False
    return JSONResponse(
        status_code=200,
        content=ok({"report": payload, "cost_hidden": not show_cost, "readonly": True, "billed": False}),
    )
