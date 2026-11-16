"""用量：`GET /usage` —— **自然月口径**（``period = YYYY-MM``）。

与 ``/entitlement``（30 天滚动窗口）**分别存储、分别展示、不得互相推导**。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.v1._shared import ids_of
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.services.entitlement import EntitlementService, current_period
from app.services.quota import QuotaService

router = APIRouter(tags=["usage"])


@router.get("/usage", summary="本月用量（自然月口径）")
def get_usage(
    context: AuthContextDep,
    session: TenantSessionDep,
    period: str | None = None,
) -> JSONResponse:
    """本月已用 / 本月额度 / 降级态；**不与权益有效期混算**。"""
    ids = ids_of(context)
    service = EntitlementService(session)
    usage = service.usage(tenant_id=ids.tenant_id, period=period or current_period())
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "usage": usage.as_dict(),
                "ledger_rows": service.ledger_count(tenant_id=ids.tenant_id),
                "caliber_note": (
                    "本口径是自然月（YYYY-MM）；权益有效期是 30 天滚动窗口，"
                    "两者分别存储、不得拼进同一个进度条"
                ),
            }
        ),
    )


@router.get("/usage/quota-bucket", summary="令牌桶余量（30/min + 1000/day）")
def get_quota_bucket(
    context: AuthContextDep,
    settings: SettingsDep,
) -> JSONResponse:
    """令牌桶当前余量（无 Redis 时为进程内实现，语义一致）。"""
    ids = ids_of(context)
    service = QuotaService.from_settings(settings)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "tenant_id": str(ids.tenant_id),
                "peek": service.peek(str(ids.tenant_id)),
                "limits": {
                    "per_minute": service.limits.per_minute,
                    "per_day": service.limits.per_day,
                },
                "degraded_to_memory": service.degraded,
            }
        ),
    )
