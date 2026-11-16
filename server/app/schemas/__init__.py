"""API 请求/响应模型（Pydantic v2）。"""

from app.schemas.auth import LoginRequest, LoginResponse

__all__ = ["LoginRequest", "LoginResponse"]
