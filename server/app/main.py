"""FastAPI 应用入口（微信云托管容器内运行，经 ``callContainer`` 私有协议暴露）。

启动行为
--------
1. 装载并强校验共享契约：38 intent / 18 slot / 5 source / 错误码 >= 20，不符 fail-fast；
2. 初始化幂等键存储（Redis 优先，失败降级内存 LRU）；
3. 注册统一包络异常处理器 + 幂等中间件 + 请求体大小守卫（100K，D11）；
4. 初始化数据层（T02）：``Database``（PG 方案 A / SQLite 本地测试），
   并如实记录本次运行的隔离能力（``db_rls`` 还是 ``app_scope_only``）。

本地启动：
    cd server && uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.v1 import (
    account,
    auth,
    cards,
    clarify,
    confirm,
    dict as dict_api,
    entitlement,
    exports,
    health,
    query,
    reversal,
    tasks,
    upload,
    usage,
    voice,
)
from app.common.envelope import failure, ok
from app.common.errors import register_exception_handlers
from app.common.idempotency import IdempotencyMiddleware, build_store
from app.config import ContractError, Settings, get_contracts, get_settings, load_contracts
from app.db.session import Database

logger = logging.getLogger(__name__)

#: 云托管 callContainer 请求体上限 100K（D11）——超限直接 E_AUDIO_TOO_LARGE
MAX_BODY_BYTES = 100 * 1024


class RequestSizeGuardMiddleware(BaseHTTPMiddleware):
    """请求体大小守卫（100K 断言）。

    音频本体绝不进私有链路：端侧直传对象存储，仅传 ``audio_object_ref``。
    """

    def __init__(self, app: Callable, max_bytes: int = MAX_BODY_BYTES) -> None:  # noqa: ANN401
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > self._max_bytes:
                spec = get_contracts().error_codes["E_AUDIO_TOO_LARGE"]
                return JSONResponse(
                    status_code=spec.http_status,
                    content=failure(
                        spec,
                        message=(
                            f"请求体 {declared} 字节超过 {self._max_bytes} 字节上限；"
                            "音频请直传对象存储，仅传 audio_object_ref"
                        ),
                    ),
                )
        return await call_next(request)


def _configure_logging(settings: Settings) -> None:
    """极简结构化日志配置（完整 structlog 接入见 T03）。"""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动日志与契约复核；契约不合法时进程直接起不来（create_app 已先校验一次）。"""
    settings: Settings = app.state.settings
    _configure_logging(settings)
    contracts = app.state.contract_bundle
    counts = contracts.counts()
    logger.info(
        "契约装载完成 intents=%d slots=%d sources=%d error_codes=%d domains=%d",
        counts["intents"],
        counts["slots"],
        counts["sources"],
        counts["error_codes"],
        counts["domains"],
    )
    database: Database = app.state.database
    capabilities = database.capabilities()
    logger.info(
        "数据层就绪 dialect=%s tables=%d rls_enabled=%s isolation_mode=%s",
        capabilities.get("dialect"),
        capabilities.get("tables"),
        capabilities.get("rls_enabled"),
        capabilities.get("isolation_mode"),
    )
    if not capabilities.get("rls_enabled"):
        # 如实提示：本地/方案 B 下少一层 DB 强制隔离，不假装 RLS 仍然生效
        logger.warning(
            "非 PostgreSQL 方言：无 DB 级 RLS，隔离强度下降（只剩 TenantRepository + "
            "唯一索引 tenant_id 打头 + CI 扫描 + 双租户越权单测）"
        )
    logger.info(
        "服务启动 env=%s version=%s（同一 AppID：手机端 + 电脑版微信大屏，无独立网页端）",
        settings.env,
        settings.version,
    )
    try:
        yield
    finally:
        database.dispose()
        logger.info("服务关闭")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用（测试可注入 Settings）。"""
    resolved = settings if settings is not None else get_settings()

    # fail-fast：契约为单一真相源，装载失败或数量不符时不允许启动
    try:
        contracts = load_contracts(resolved.contracts_path())
    except ContractError as exc:
        logger.critical("共享契约校验失败，拒绝启动：%s", exc)
        raise

    app = FastAPI(
        title="语音录入版轻量化进销存 · 云托管服务",
        version=resolved.version,
        description=(
            "T01 共享契约 + /healthz；T02 数据层（34 张表 + PG RLS）与登录 "
            "（POST /api/v1/auth/login）；语音解析流水线见 T03，写账与虚拟支付见 T04。"
        ),
        lifespan=lifespan,
        docs_url=None if resolved.env == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.env == "prod" else "/openapi.json",
    )
    app.state.settings = resolved
    app.state.contract_bundle = contracts
    app.state.idempotency_store = build_store(resolved)
    # 数据层：PG 走方案 A（RLS），本地/测试可落到 SQLite（无 RLS，capabilities 会如实说明）
    app.state.database = Database.from_url(resolved.database_url, echo=resolved.database_echo)

    register_exception_handlers(app)
    app.add_middleware(RequestSizeGuardMiddleware, max_bytes=resolved.max_request_body_bytes)
    # 注意：后注册的中间件先执行；幂等中间件包在外层，先占位再交给内层处理
    app.add_middleware(IdempotencyMiddleware, store=app.state.idempotency_store)

    app.include_router(health.router)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    # ---- T04：语音记账链（写操作唯一入口是 confirm）
    app.include_router(voice.router, prefix="/api/v1")
    app.include_router(clarify.router, prefix="/api/v1")
    app.include_router(cards.router, prefix="/api/v1")
    app.include_router(confirm.router, prefix="/api/v1")
    app.include_router(reversal.router, prefix="/api/v1")
    # ---- 只读查询与用量
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(usage.router, prefix="/api/v1")
    # ---- 词表 / 导出 / 待办 / 账号
    app.include_router(dict_api.router, prefix="/api/v1")
    app.include_router(exports.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(account.router, prefix="/api/v1")
    # ---- 虚拟支付与权益（与 voice_session 状态机相互独立）
    app.include_router(entitlement.router, prefix="/api/v1")
    app.include_router(upload.router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        """服务根路径：给出能力说明，避免误以为存在网页端。"""
        capabilities = app.state.database.capabilities()
        return JSONResponse(
            status_code=200,
            content=ok(
                {
                    "service": resolved.app_name,
                    "version": resolved.version,
                    "health": "/healthz",
                    "ready": "/readyz",
                    "login": "/api/v1/auth/login",
                    "isolation_mode": capabilities.get("isolation_mode"),
                    "memo": "无独立网页端；小程序（手机端 + 电脑版微信大屏）经 callContainer 调用本服务",
                }
            ),
        )

    return app


app = create_app()
