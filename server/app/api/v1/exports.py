"""导出（R3）：`POST /exports`、`GET /exports` —— **只在电脑版微信大屏发起**。

不提供网页下载：文件生成后经微信转发 / 保存 / 预览交付（本项目无网页端）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import BOSS_ONLY, assert_writable, ids_of, require_roles
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.services.export_job import EXPORT_SCOPES, ExportService

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportCreateRequest(BaseModel):
    """导出入参。"""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(default="docs", description="docs / stock / debt / audit / usage")
    file_format: str = Field(default="csv", description="csv / xlsx")
    period: str | None = Field(default=None, max_length=32)


@router.post("", summary="创建导出任务（R3，仅老板 + 大屏）")
def create_export(
    payload: ExportCreateRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """创建异步导出任务；**幂等键必填或由服务端派生**。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

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
    """列出导出任务（含交付方式说明）。"""
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
