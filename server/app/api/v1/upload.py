"""音频上传票据：`POST /upload/ticket` —— 端侧直传，**服务端只收引用**。

100KiB 断言**下沉到这里**，与 ``RequestSizeGuardMiddleware``、``/voice/*``
复用同一个常量（``settings.audio_max_bytes``），不写第二份阈值。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep
from app.services.upload_ticket import UploadTicketRequest, UploadTicketService, reject_audio_payload

router = APIRouter(prefix="/upload", tags=["upload"])


class TicketBody(BaseModel):
    """票据入参（**没有、也不允许有音频字段**）。"""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID | None = Field(default=None)
    duration_ms: int = Field(default=0, ge=0, le=60_000)
    client_bytes: int | None = Field(default=None, ge=0)
    codec: str = Field(default="silk", max_length=16)


@router.post("/ticket", summary="申请音频上传票据（只收引用，不收字节）")
def issue_ticket(
    payload: TicketBody,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """签发短期上传票据；超 100KiB 当场拒（``E_AUDIO_TOO_LARGE``）。

    Returns:
        包络 ``data: {ticket_id, audio_object_ref, upload_prefix, expires_at, max_bytes}``
    """
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    # 第二道防线：入参里出现音频字段就直接拒（Pydantic 之外再拦一次）
    reject_audio_payload(payload.model_dump())

    service = UploadTicketService(
        max_bytes=settings.audio_max_bytes,
        ttl_seconds=settings.upload_ticket_ttl_seconds,
    )
    ticket = service.issue(
        UploadTicketRequest(
            duration_ms=payload.duration_ms,
            client_bytes=payload.client_bytes,
            codec=payload.codec,
        ),
        tenant_id=str(ids.tenant_id),
        member_id=str(ids.member_id),
        session_id=None if payload.session_id is None else str(payload.session_id),
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "ticket_id": ticket.ticket_id,
                "audio_object_ref": ticket.audio_object_ref,
                "upload_prefix": ticket.upload_prefix,
                "expires_at": ticket.expires_at.isoformat(),
                "max_bytes": settings.audio_max_bytes,
                "cleanup": "临时对象短 TTL + 限期删除（需核实合规）；音频默认不留存",
                "note": "请用 wx.cloud.uploadFile 直传该前缀；callContainer 只传引用",
            }
        ),
    )
