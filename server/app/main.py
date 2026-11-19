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
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
from app.tenant import rls

logger = logging.getLogger(__name__)

#: 云托管 callContainer 请求体上限 100K（D11）——超限直接 E_AUDIO_TOO_LARGE
MAX_BODY_BYTES = 100 * 1024


class _BodyTooLarge(Exception):
    """内部信号：请求体在流式读取过程中超过上限。

    只在 :class:`RequestSizeGuardMiddleware` 内部使用，**不对外暴露**——
    它从 ``receive()`` 里抛出，穿过下游业务代码，被中间件自己接住并转成 413。
    """

    def __init__(self, total_bytes: int) -> None:
        super().__init__(f"请求体已累计 {total_bytes} 字节，超过上限")
        self.total_bytes = total_bytes


class RequestSizeGuardMiddleware:
    """请求体大小守卫（100K 断言）——**纯 ASGI 中间件**。

    为什么是纯 ASGI（不是 ``BaseHTTPMiddleware``）
    --------------------------------------------
    ``BaseHTTPMiddleware`` 会把下游应用放进**另一个 task** 跑，于是
    ``bind()``（在下游 task）与 ``reset()``（在中间件 task）落在不同 context 里，
    租户上下文可能错位——而错位的后果是多租户串数据，不是报错。
    纯 ASGI 形态下 ``__call__`` 与下游在**同一个 task**，从根上不产生这个问题。

    行为约束
    --------
    - **不一次性缓冲整个 body**：包装 ``receive``，边读边累计字节数；
    - 一旦超限**立即抛 :class:`_BodyTooLarge` 中断下游**，由本中间件回 413，
      不再继续读剩下的字节（省带宽也省内存）；
    - ``content-length`` 头已声明超限时直接拒绝，连下游都不进；
    - 非 ``http`` scope（``lifespan`` / ``websocket``）原样透传。

    音频本体绝不进本链路：端侧直传对象存储，仅传 ``audio_object_ref``。
    """

    def __init__(self, app: Callable, max_bytes: int = MAX_BODY_BYTES) -> None:  # noqa: ANN401
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await self._reject(scope, receive, send, total_bytes=declared)
            return

        total = 0

        async def limited_receive() -> Any:  # noqa: ANN401
            """边读边累计；超限立刻中断（不再把剩余字节喂给下游）。"""
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self._max_bytes:
                    raise _BodyTooLarge(total)
            return message

        response_started = False

        async def tracking_send(message: Any) -> None:  # noqa: ANN401
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge as exc:
            if response_started:
                # 已经回了一部分响应（流式场景）：改不了状态码，如实记录
                logger.warning(
                    "请求体超限但响应已开始发送，无法改回 413（已读 %d 字节，上限 %d）",
                    exc.total_bytes,
                    self._max_bytes,
                )
                raise
            await self._reject(scope, receive, send, total_bytes=exc.total_bytes)

    async def _reject(
        self,
        scope: Any,  # noqa: ANN401
        receive: Any,  # noqa: ANN401
        send: Any,  # noqa: ANN401
        *,
        total_bytes: int,
    ) -> None:
        """回 413 + ``E_AUDIO_TOO_LARGE``（直接作为 ASGI 应用发送，不经下游）。"""
        spec = get_contracts().error_codes["E_AUDIO_TOO_LARGE"]
        response = JSONResponse(
            status_code=spec.http_status,
            content=failure(
                spec,
                message=(
                    f"请求体 {total_bytes} 字节超过 {self._max_bytes} 字节上限；"
                    "音频请直传对象存储，仅传 audio_object_ref"
                ),
            ),
        )
        await response(scope, receive, send)


def _declared_length(scope: Any) -> int | None:  # noqa: ANN401
    """从 ASGI scope 里读 ``content-length``（没有 / 非法都返回 ``None``）。"""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"content-length":
            try:
                return int(raw_value)
            except (TypeError, ValueError):
                return None
    return None


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
    if settings.is_production() and settings.llm_is_fake():
        # 不拒绝启动（规则层是主路径），但必须显式告知：需要模型的说法会转手动
        logger.warning(
            "生产未配置大模型（LLM_PROVIDER=fake）：仅规则层可用 "
            "（进货/卖货/查询/新建商品/别名）；撤销/更正/教说法会降级为澄清或手动录入。"
            "要开启模型：LLM_PROVIDER=real + LLM_API_KEY / LLM_BASE_URL"
        )
    try:
        yield
    finally:
        database.dispose()
        logger.info("服务关闭")


def _assert_production_config(settings: Settings, database: Database) -> None:
    """生产环境自检：**错配就拒绝启动**，不带病上线。

    为什么必须在启动期炸，而不是等用户发现
    --------------------------------------
    这三类错配的共同点是「服务能起来、接口也返回 200」，但语义是错的：

    1. ``DATABASE_URL`` 落到 SQLite：没有 RLS（多租户隔离少一层），且内存库重启即丢数据；
    2. ``WX_PROVIDER=fake``：任何 code 都能换出一个 openid → **任何人可开通租户**；
    3. ``JWT_SECRET`` 还是默认值：token 可被任意伪造。

    LLM / 支付已经有同构的「生产禁止 fake」保护（``llm_provider`` / ``pay/sign.py``），
    这里把数据层与登录补齐，保持同一条纪律。
    """
    if not settings.is_production():
        return

    problems: list[str] = []
    # 只读方言、**不连库**：用 rls.capabilities(engine) 而不是 database.capabilities()，
    # 后者会去查实际表数（DB 不可达时会等到建连超时，启动自检就被拖死）。
    dialect = str(rls.capabilities(database.engine).get("dialect") or "")
    if dialect != "postgresql":
        problems.append(
            f"DATABASE_URL 不是 PostgreSQL（dialect={dialect!r}）：生产必须是 PG，"
            "否则没有 DB 级 RLS、重启还会丢数据"
        )
    if (settings.wx_provider or "").strip().lower() != "real":
        problems.append(
            f"WX_PROVIDER={settings.wx_provider!r}：生产必须 real，"
            "fake 会允许任何人用任意 code 开通租户"
        )
    if settings.jwt_secret_is_default():
        problems.append("JWT_SECRET 仍是本地默认值：任何人都能伪造登录凭证")

    if problems:
        raise RuntimeError(
            "生产配置自检未通过，拒绝启动：\n- " + "\n- ".join(problems)
        )


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
    # 生产自检：非 PG / fake 登录 / 默认 JWT 密钥 → 直接拒绝启动（见函数 docstring）
    _assert_production_config(resolved, app.state.database)

    register_exception_handlers(app)
    app.add_middleware(RequestSizeGuardMiddleware, max_bytes=resolved.max_request_body_bytes)
    # 注意：后注册的中间件先执行；幂等中间件包在外层，先占位再交给内层处理。
    # 必须传 settings：幂等键的租户命名空间要从 JWT claims 解（否则全部落进 ``anon``，
    # A 商户的 export 结果会被重放给 B 商户）。
    app.add_middleware(
        IdempotencyMiddleware, store=app.state.idempotency_store, settings=resolved
    )

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
