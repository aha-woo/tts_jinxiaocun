"""登录接口：`POST /api/v1/auth/login`（`code` -> openid -> tenant_id -> JWT）。

注意：**入参不含 tenant_id**（防伪造）；租户一律由服务端根据 openid 解析。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.common.envelope import ok
from app.config import Settings
from app.deps import get_app_settings, get_database
from app.schemas.auth import LoginRequest
from app.services.auth import AuthService
from app.services.wx_provider import build_wx_provider
from app.utils.date_utils import to_utc_iso

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", summary="微信登录（code -> JWT）")
def login(
    payload: LoginRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> JSONResponse:
    """用 `wx.login` 的 code 登录；首次登录自动开通租户（单商户 MVP）。

    Returns:
        包络 ``{code:0, data:{jwt, token_type, expires_at, tenant_id, store_id, member_id, role,
        tenant_version, plan}}``
    """
    database = get_database(request)
    service = AuthService(database, settings, build_wx_provider(settings))
    result = service.login(payload.code, payload.store_hint)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "jwt": result.token,
                "token_type": "Bearer",
                "expires_at": to_utc_iso(result.expires_at),
                "tenant_id": str(result.tenant_id),
                "store_id": str(result.store_id),
                "member_id": str(result.member_id),
                "role": result.role,
                "tenant_version": result.tenant_version,
                "plan": result.plan,
            }
        ),
    )
