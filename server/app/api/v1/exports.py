"""导出（R3）：`POST /exports`、`GET /exports` —— **只在电脑版微信大屏发起**。

不提供网页下载：文件生成后经微信转发 / 保存 / 预览交付（本项目无网页端）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import BOSS_ONLY, ids_of, require_roles
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.models.tenant import Tenant
from app.services.export_job import EXPORT_SCOPES, ExportService

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportCreateRequest(BaseModel):
    """导出入参。"""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(default="docs", description="docs / stock / debt / audit / usage")
    file_format: str = Field(default="csv", description="csv / xlsx")
    period: str | None = Field(default=None, max_length=32)


@router.post("", summary="创建导出任务（R3，仅老板 + 大屏；注销冷静期允许导出）")
def create_export(
    payload: ExportCreateRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """创建异步导出任务；注销冷静期内也放行导出（不拿数据当人质）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    tenant = session.get(Tenant, ids.tenant_id)
    if tenant is not None and str(tenant.status) == "frozen":
        raise AppError("E_TENANT_DENIED", message="账号已冻结，暂不支持导出")

    service = ExportService(
        session, signed_url_ttl_seconds=settings.export_signed_url_ttl_seconds
    )
    outcome = service.create(
        tenant_id=ids.tenant_id,
        member_id=ids.member_id,
        scope=payload.scope,
        file_format=payload.file_format,
        period=payload.period,
        idempotency_key=idempotency_key or f"{ids.member_id}:{payload.scope}:{payload.period}",
    )
    if outcome.status != "done":
        _, row_count = service.render_csv(
            tenant_id=ids.tenant_id,
            scope=payload.scope,
            period=payload.period,
        )
        outcome = service.complete(
            tenant_id=ids.tenant_id,
            job_id=uuid.UUID(outcome.job_id),
            row_count=row_count,
            member_id=ids.member_id,
        )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "export": outcome.as_dict(),
                "allowed_scopes": list(EXPORT_SCOPES),
                "note": "R3：语音侧只生成待办；本接口由电脑版微信大屏管理页调用",
            }
        ),
    )


@router.get("", summary="我的导出任务")
def list_exports(
    context: AuthContextDep,
    session: TenantSessionDep,
    limit: int = 20,
) -> JSONResponse:
    """列出导出任务（仅老板可看）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    rows = ExportService(session).list_jobs(tenant_id=ids.tenant_id, limit=limit)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [row.as_dict() for row in rows],
                "delivery": "经微信转发 / 保存 / 预览交付，不提供网页下载",
            }
        ),
    )


@router.get("/{job_id}/content", summary="读取导出 CSV 内容（仅老板，经 callContainer 下发微信本地文件）")
def get_export_content(
    job_id: str,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """返回已校验租户的 UTF-8 BOM CSV 内容供小程序写本地文件并微信转发。"""
    require_roles(context, BOSS_ONLY)
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="job_id 格式不合法") from exc
    ids = ids_of(context)
    service = ExportService(session)
    job = service.get_job(tenant_id=ids.tenant_id, job_id=job_uuid)
    csv_content, row_count = service.render_csv(
        tenant_id=ids.tenant_id,
        scope=str(job.scope),
        period=job.period,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "job_id": str(job.job_id),
                "scope": str(job.scope),
                "filename": f"export_{job.scope}_{str(job.job_id)[:8]}.csv",
                "row_count": row_count,
                "mime": "text/csv;charset=utf-8",
                "csv_content": csv_content,
            }
        ),
    )
