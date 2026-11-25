"""R3 待办：`GET /tasks`、`POST /tasks/{id}/approve` —— **只能在电脑版微信大屏执行**。

手机端与语音端一律 ``E_R3_PC_REQUIRED``(1206)；角色不对 ``E_ROLE_DENIED``(1302)。
本项目**没有网页端**，提示里绝不引导到外部浏览器 / H5。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import BIG_SCREEN_HINT, BOSS_ONLY, ids_of, require_roles
from app.common.envelope import ok
from app.deps import AuthContextDep, TenantSessionDep
from app.services.pending_task import BIG_SCREEN_DEVICE, PendingTaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


class ApproveRequest(BaseModel):
    """审批入参（大屏侧二次确认）。"""

    model_config = ConfigDict(extra="forbid")

    approve: bool = Field(default=True)
    second_confirm: bool = Field(default=False, description="大屏上必须做过二次确认")
    device: str = Field(default=BIG_SCREEN_DEVICE, description="只接受 pc_bigscreen")


@router.get("", summary="待审批列表（大屏审批箱）")
def list_tasks(
    context: AuthContextDep,
    session: TenantSessionDep,
    limit: int = 50,
) -> JSONResponse:
    """R3 待办列表：**只有老板可看，只收 R3 高风险动作**。"""
    require_roles(context, BOSS_ONLY)
    ids = ids_of(context)
    rows = PendingTaskService(session).list_pending(tenant_id=ids.tenant_id, limit=limit)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [row.as_dict() for row in rows],
                "hint_zh": BIG_SCREEN_HINT,
                "note": "该队列只收 R3 高风险动作（合并/换算系数/导出/销户），不收「没听清」",
            }
        ),
    )


@router.post("/{task_id}/approve", summary="审批 R3 待办（仅 boss + 大屏 + 二次确认）")
def approve_task(
    task_id: uuid.UUID,
    payload: ApproveRequest,
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """审批：角色 → 设备 → 二次确认，三道全过才执行。"""
    ids = ids_of(context)
    decision = PendingTaskService(session).decide(
        tenant_id=ids.tenant_id,
        task_id=task_id,
        approve=payload.approve,
        role=context.role_code,
        device=payload.device,
        second_confirm=payload.second_confirm,
        member_id=ids.member_id,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "decision": decision.as_dict(),
                "hint_zh": BIG_SCREEN_HINT,
                "idempotency_key": idempotency_key or str(task_id),
            }
        ),
    )
