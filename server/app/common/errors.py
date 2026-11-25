"""业务异常与 FastAPI 异常处理器（把异常统一映射为包络）。

约定（架构 §8.4）：门禁/过滤类不扣次；``E_R3_PC_REQUIRED`` 必须引导
「电脑版微信大屏管理页」，禁止引导到外部浏览器/H5（本项目无网页端）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.envelope import failure
from app.config import ContractBundle, get_contracts

#: 未登记错误码时的兜底（绝不让未知异常裸奔成 500 纯文本）
FALLBACK_ERROR_CODE = "E_INTERNAL"


class AppError(Exception):
    """可被包络化的业务异常。

    Parameters
    ----------
    code:
        字符串错误码（必须已登记在 error-codes.json）。
    message:
        覆盖默认中文提示（面向用户的人话）。
    data:
        附加信息（如待办 id、冲突版本号），会并入包络 ``data``。
    http_status:
        覆盖默认 HTTP 状态码（一般不需要）。
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        data: Any = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status
        self.contracts: ContractBundle = get_contracts()
        spec = self.contracts.error_codes.get(code)
        if spec is None:
            # 未登记码不允许静默通过：降级为 E_INTERNAL，但保留原始 code 供排查
            fallback = self.contracts.error_codes[FALLBACK_ERROR_CODE]
            self._spec = fallback
            self.resolved_code = FALLBACK_ERROR_CODE
            self.resolved_message = message or f"未登记错误码 {code}"
            self.resolved_status = http_status or fallback.http_status
        else:
            self._spec = spec
            self.resolved_code = spec.code
            self.resolved_message = message or spec.message_zh
            self.resolved_status = http_status or spec.http_status
        super().__init__(f"[{self.resolved_code}] {self.resolved_message}")

    def spec(self):
        """返回用于构造包络的错误码定义。"""
        return self._spec

    def to_response(self) -> JSONResponse:
        """转成 JSONResponse（包络 + HTTP 状态码）。"""
        payload = failure(self._spec, message=self.resolved_message, data=self.data)
        payload["data"] = dict(payload["data"] or {})
        payload["data"]["error_code"] = self.resolved_code
        if self.code != self.resolved_code:
            payload["data"]["original_error_code"] = self.code
        return JSONResponse(status_code=self.resolved_status, content=payload)


def _envelope_from_spec(contracts: ContractBundle, code: str, message: str | None = None) -> tuple[dict[str, Any], int]:
    """按字符串码构造 (包络 dict, HTTP 状态码)。"""
    spec = contracts.error_codes.get(code) or contracts.error_codes[FALLBACK_ERROR_CODE]
    return failure(spec, message=message), spec.http_status


def register_exception_handlers(app: FastAPI) -> None:
    """注册 AppError / 校验失败 / 404 等处理器，保证任何响应都是包络。"""
    from app.services.upload_ticket import AudioTooLargeError
    from app.tenant.context import TenantContextMissing

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        contracts = get_contracts()
        payload, status = _envelope_from_spec(contracts, "E_VALIDATION")
        payload["data"] = {"error_code": "E_VALIDATION", "errors": exc.errors()}
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        contracts = get_contracts()
        if exc.status_code == 404:
            code = "E_NOT_FOUND"
        elif exc.status_code < 500:
            code = "E_VALIDATION"
        else:
            code = FALLBACK_ERROR_CODE
        payload, _spec_status = _envelope_from_spec(
            contracts,
            code,
            None if code == "E_NOT_FOUND" else str(exc.detail),
        )
        # 保留原始 HTTP 状态码（404 仍是 404），包络 code 用契约 numeric
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(AudioTooLargeError)
    async def _handle_audio_too_large(_request: Request, exc: AudioTooLargeError) -> JSONResponse:
        contracts = get_contracts()
        payload, status = _envelope_from_spec(contracts, "E_AUDIO_TOO_LARGE", str(exc))
        payload["data"] = {"error_code": "E_AUDIO_TOO_LARGE"}
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(TenantContextMissing)
    async def _handle_tenant_missing(_request: Request, exc: TenantContextMissing) -> JSONResponse:
        contracts = get_contracts()
        payload, status = _envelope_from_spec(contracts, "E_TENANT_DENIED", exc.detail)
        payload["data"] = {"error_code": "E_TENANT_DENIED"}
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        contracts = get_contracts()
        payload, status = _envelope_from_spec(contracts, FALLBACK_ERROR_CODE)
        payload["data"] = {"error_code": FALLBACK_ERROR_CODE}
        return JSONResponse(status_code=status, content=payload)
