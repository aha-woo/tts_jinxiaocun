"""登录相关 DTO（/auth/login）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """`wx.login` 拿到的一次性 code。"""

    code: str = Field(min_length=1, max_length=512, description="wx.login 的 code（一次性，5 分钟有效）")
    #: 可选门店提示（一人多店时用；MVP 单店可不传）。**不传 tenant_id**（防伪造）
    store_hint: str | None = Field(default=None, max_length=64)


class LoginResponse(BaseModel):
    """登录结果：JWT + 身份要素（前端只用于展示，鉴权仍以服务端 JWT 为准）。"""

    jwt: str
    token_type: str = Field(default="Bearer")
    expires_at: str = Field(description="ISO 8601 UTC")
    tenant_id: str
    store_id: str
    member_id: str
    role: str = Field(description="boss / manager / clerk / readonly")
    tenant_version: int
    plan: str = Field(description="free / 30day / quota_pack")
