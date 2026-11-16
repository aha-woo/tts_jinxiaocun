"""只读查询：`GET /query/{stock,docs,debt,report}` —— **R0 不写库、不占卡片、不扣次**。

这些端点存在的意义就是「不给确认卡片」：查个库存还要点确认，用户会疯。
所以它们直接返回结果，并且**绝不**碰 ``usage_ledger`` 或 ``confirm_card``。
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.v1._shared import ids_of
from app.common.envelope import ok
from app.deps import AuthContextDep, TenantSessionDep
from app.domain.report_service import ReportService

router = APIRouter(prefix="/query", tags=["query"])


@router.get("/stock", summary="查库存（R0）")
def query_stock(
    context: AuthContextDep,
    session: TenantSessionDep,
    name: str | None = None,
    low_only: bool = False,
    limit: int = 20,
) -> JSONResponse:
    """按商品名模糊查库存；``low_only`` 只看低库存。"""
    ids = ids_of(context)
    service = ReportService(session)
    rows = (
        service.low_stock(tenant_id=ids.tenant_id, store_id=ids.store_id, limit=limit)
        if low_only
        else service.stock_of(
            tenant_id=ids.tenant_id, store_id=ids.store_id, product_name=name, limit=limit
        )
    )
    return JSONResponse(
        status_code=200,
        content=ok({"items": [row.as_dict() for row in rows], "readonly": True, "billed": False}),
    )


@router.get("/docs", summary="查单据（R0）")
def query_docs(
    context: AuthContextDep,
    session: TenantSessionDep,
    biz_date: date | None = None,
    doc_type: str | None = None,
    limit: int = 20,
) -> JSONResponse:
    """按日期/类型查单据列表。"""
    ids = ids_of(context)
    rows = ReportService(session).docs(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        biz_date=biz_date,
        doc_type=doc_type,
        limit=limit,
    )
    return JSONResponse(
        status_code=200,
        content=ok({"items": [row.as_dict() for row in rows], "readonly": True, "billed": False}),
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
    top: bool = False,
) -> JSONResponse:
    """今日快报 / 销量 Top N。"""
    ids = ids_of(context)
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
    report = service.today(
        tenant_id=ids.tenant_id, store_id=ids.store_id, biz_date=biz_date or date.today()
    )
    return JSONResponse(
        status_code=200,
        content=ok({"report": report.as_dict(), "readonly": True, "billed": False}),
    )
