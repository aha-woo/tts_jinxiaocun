"""统一响应包络 `{code, data, message}`（架构 §8.4）。

- ``code = 0`` 成功；
- 失败时 ``code`` 取 error-codes.json 的 ``numeric``，``data.error_code`` 带字符串码；
- 端侧 ``packages/shared/contracts/api-envelope.ts`` 是同一契约的 TS 版本。
"""

from __future__ import annotations

from typing import Any, Generic, Mapping, TypeVar

from pydantic import BaseModel, Field

from app.config import ErrorSpec

T = TypeVar("T")

#: 成功码，与端侧 SUCCESS_CODE 保持一致
SUCCESS_CODE = 0


class ApiEnvelope(BaseModel, Generic[T]):
    """统一响应包络。"""

    code: int = Field(default=SUCCESS_CODE, description="0=成功，其余见 error-codes.json 的 numeric")
    data: T | None = Field(default=None, description="业务数据；失败时为错误详情")
    message: str = Field(default="ok", description="人话提示，可直接展示")


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """构造成功包络（dict，交给 JSONResponse 或直接 return）。"""
    return {"code": SUCCESS_CODE, "data": data, "message": message}


def failure(spec: ErrorSpec, message: str | None = None, data: Any = None) -> dict[str, Any]:
    """构造失败包络，``data`` 中至少带 ``error_code`` 便于前端分支。"""
    payload: dict[str, Any] = {"error_code": spec.code, "category": spec.category}
    if isinstance(data, Mapping):
        payload.update(dict(data))
    elif data is not None:
        payload["detail"] = data
    return {
        "code": spec.numeric,
        "data": payload,
        "message": message if message is not None else spec.message_zh,
    }


def unwrap_success(envelope: Mapping[str, Any]) -> Any:
    """从包络里取 data；非成功码直接抛 ValueError（服务端内部调用使用）。"""
    if int(envelope.get("code", -1)) != SUCCESS_CODE:
        raise ValueError(f"包络不是成功响应: {envelope.get('message')}")
    return envelope.get("data")
