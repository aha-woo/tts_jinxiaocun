"""账号：`POST /account/deletion`、`DELETE /account/deletion`、`GET /account/deletion`。

销户是 R3（**完全禁止语音**）：7 天冷静期、冷静期内只读冻结、可撤销。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import BOSS_ONLY, ids_of, require_roles
from app.common.envelope import ok
from app.deps import AuthContextDep, TenantSessionDep
from app.services.deletion_request import DeletionService

router = APIRouter(prefix="/account", tags=["account"])


class DeletionRequestPayload(BaseModel):
    """销户申请入参（**必须显式确认**）。"""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(default=False, description="必须为 true，防误触")
    reason: str = Field(default="", max_length=200)


@router.post("/deletion", summary="申请销户（7 天冷静期，只读冻结）")
def request_deletion(
    payload: DeletionRequestPayload,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """提交销户申请；``confirm=false`` 直接拒（防误触）。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    if not payload.confirm:
        from app.common.errors import AppError

        raise AppError("E_VALIDATION", message="销户必须显式确认（confirm=true）")
    outcome = DeletionService(session).request(
        tenant_id=ids.tenant_id, member_id=ids.member_id, reason=payload.reason
    )
    return JSONResponse(status_code=200, content=ok({"deletion": outcome.as_dict()}))


@router.delete("/deletion", summary="撤销销户（立刻解除冻结）")
def cancel_deletion(
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """撤销销户，恢复正常写操作。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    outcome = DeletionService(session).cancel(
        tenant_id=ids.tenant_id, member_id=ids.member_id
    )
    return JSONResponse(status_code=200, content=ok({"deletion": outcome.as_dict()}))


@router.get("/deletion", summary="当前销户状态")
def get_deletion(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """查看是否处于冷静期（前端据此禁用记账按钮）。"""
    ids = ids_of(context)
    state = DeletionService(session).current(tenant_id=ids.tenant_id)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "deletion": state,
                "note": "冷静期内只能查看与导出，不能记账；随时可撤销",
            }
        ),
    )
