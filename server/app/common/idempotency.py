"""幂等键中间件（网关层 `SETNX` + 内存 LRU 自动降级）。

契约（架构 §8.4 / T01 验收 ④）
-----------------------------
- 写接口统一要求请求头 ``Idempotency-Key``；
- 首次请求：占位 -> 执行 -> 缓存结果（TTL 60s）；
- 重复请求（已完成）：直接返回首次结果，**不重复执行业务**；
- 重复请求（处理中）：返回 ``E_DUPLICATE_REQUEST``（409）；
- Redis 不可用（云托管不部署有状态服务，见 D7/§4.5）时自动降级内存 LRU，
  进程内仍能做到「同一秒内连点两次只落一次账」。

注意：这里是**网关层**幂等；DB 唯一约束（``card_id`` / ``outTradeNo``）是第二道保险，
由 T02/T04 实现，两者必须并存，不得只靠其中一层。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.common.envelope import failure
from app.config import Settings, get_contracts

logger = logging.getLogger(__name__)

#: 需要幂等的 HTTP 方法（查询天然幂等，不加锁）
IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

STATE_NEW = "new"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"


@dataclass(frozen=True)
class CachedResponse:
    """首次执行的响应快照（仅缓存 JSON，避免缓存大对象/文件流）。"""

    status_code: int
    body: bytes
    media_type: str
    headers: dict[str, str]

    def to_json_payload(self) -> dict[str, Any]:
        """序列化为可存入 Redis 的 dict。"""
        return {
            "status_code": self.status_code,
            "body": self.body.decode("utf-8", errors="replace"),
            "media_type": self.media_type,
            "headers": self.headers,
        }

    @classmethod
    def from_json_payload(cls, payload: dict[str, Any]) -> "CachedResponse":
        """从 Redis 取回的快照还原。"""
        return cls(
            status_code=int(payload["status_code"]),
            body=str(payload["body"]).encode("utf-8"),
            media_type=str(payload.get("media_type", "application/json")),
            headers=dict(payload.get("headers", {})),
        )


class IdempotencyStore(Protocol):
    """幂等存储接口（内存 / Redis 两种实现）。"""

    async def begin(self, key: str) -> str:
        """占位；返回 ``new`` / ``in_progress`` / ``completed``。"""

    async def get(self, key: str) -> CachedResponse | None:
        """取首次执行结果。"""

    async def complete(self, key: str, response: CachedResponse) -> None:
        """写入首次执行结果。"""

    async def release(self, key: str) -> None:
        """释放占位（执行失败时允许客户端重试）。"""

    async def describe(self) -> dict[str, Any]:
        """暴露后端类型（健康检查/排障用）。"""


class MemoryLRUStore:
    """进程内 LRU + TTL 存储（Redis 不可用时的降级实现）。"""

    def __init__(self, ttl_seconds: int = 60, max_entries: int = 2048) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._lock = asyncio.Lock()

    def _purge_expired(self, now: float) -> None:
        """清理过期条目（惰性清理，避免后台任务）。"""
        expired = [key for key, entry in self._entries.items() if entry["expires_at"] <= now]
        for key in expired:
            self._entries.pop(key, None)

    async def begin(self, key: str) -> str:
        async with self._lock:
            now = time.monotonic()
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return STATE_COMPLETED if entry["state"] == STATE_COMPLETED else STATE_IN_PROGRESS
            self._entries[key] = {
                "state": STATE_IN_PROGRESS,
                "expires_at": now + self._ttl,
                "response": None,
            }
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return STATE_NEW

    async def get(self, key: str) -> CachedResponse | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry["state"] != STATE_COMPLETED:
                return None
            return entry["response"]

    async def complete(self, key: str, response: CachedResponse) -> None:
        async with self._lock:
            self._entries[key] = {
                "state": STATE_COMPLETED,
                "expires_at": time.monotonic() + self._ttl,
                "response": response,
            }
            self._entries.move_to_end(key)

    async def release(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def describe(self) -> dict[str, Any]:
        return {
            "backend": "memory",
            "ttl_seconds": self._ttl,
            "max_entries": self._max_entries,
            "size": len(self._entries),
        }


class RedisIdempotencyStore:
    """基于 Redis SETNX 的幂等存储（可用时优先使用）。"""

    def __init__(self, url: str, ttl_seconds: int = 60, prefix: str = "vil:idem") -> None:
        # 延迟导入：未安装 redis 包时也能用内存降级
        from redis import asyncio as redis_asyncio  # noqa: PLC0415

        self._client = redis_asyncio.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def begin(self, key: str) -> str:
        redis_key = self._key(key)
        payload = json.dumps({"state": STATE_IN_PROGRESS})
        created = await self._client.set(redis_key, payload, nx=True, ex=self._ttl)
        if created:
            return STATE_NEW
        existing = await self._client.get(redis_key)
        if existing is None:  # 竞态：刚好过期
            created_retry = await self._client.set(redis_key, payload, nx=True, ex=self._ttl)
            return STATE_NEW if created_retry else STATE_IN_PROGRESS
        try:
            state = json.loads(existing).get("state", STATE_IN_PROGRESS)
        except json.JSONDecodeError:
            state = STATE_IN_PROGRESS
        return STATE_COMPLETED if state == STATE_COMPLETED else STATE_IN_PROGRESS

    async def get(self, key: str) -> CachedResponse | None:
        raw = await self._client.get(self._key(key))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if payload.get("state") != STATE_COMPLETED or "response" not in payload:
            return None
        return CachedResponse.from_json_payload(payload["response"])

    async def complete(self, key: str, response: CachedResponse) -> None:
        payload = json.dumps({"state": STATE_COMPLETED, "response": response.to_json_payload()})
        await self._client.set(self._key(key), payload, ex=self._ttl)

    async def release(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def describe(self) -> dict[str, Any]:
        return {"backend": "redis", "ttl_seconds": self._ttl, "prefix": self._prefix}


class HybridStore:
    """Redis 优先、失败自动降级内存，并记录降级状态。"""

    def __init__(
        self,
        primary: IdempotencyStore | None,
        fallback: MemoryLRUStore,
        primary_expected: bool = False,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        # 只有「本应使用 Redis 却用不上」才算降级；本地/未配置 Redis 时的内存是正常模式
        self._degraded = primary is None and primary_expected
        self._lock = asyncio.Lock()

    @property
    def degraded(self) -> bool:
        """是否处于内存降级态。"""
        return self._degraded

    async def _disable_primary(self, exc: Exception, action: str) -> None:
        async with self._lock:
            if self._primary is not None:
                logger.warning("幂等存储降级为内存：%s 失败（%s）", action, exc)
            self._primary = None
            self._degraded = True

    async def begin(self, key: str) -> str:
        if self._primary is not None:
            try:
                return await self._primary.begin(key)
            except Exception as exc:  # noqa: BLE001 - 任何 Redis 故障都必须降级而不是 500
                await self._disable_primary(exc, "begin")
        return await self._fallback.begin(key)

    async def get(self, key: str) -> CachedResponse | None:
        if self._primary is not None:
            try:
                return await self._primary.get(key)
            except Exception as exc:  # noqa: BLE001
                await self._disable_primary(exc, "get")
        return await self._fallback.get(key)

    async def complete(self, key: str, response: CachedResponse) -> None:
        if self._primary is not None:
            try:
                await self._primary.complete(key, response)
                return
            except Exception as exc:  # noqa: BLE001
                await self._disable_primary(exc, "complete")
        await self._fallback.complete(key, response)

    async def release(self, key: str) -> None:
        if self._primary is not None:
            try:
                await self._primary.release(key)
                return
            except Exception as exc:  # noqa: BLE001
                await self._disable_primary(exc, "release")
        await self._fallback.release(key)

    async def describe(self) -> dict[str, Any]:
        backend = "memory"
        if self._primary is not None:
            try:
                return await self._primary.describe()
            except Exception:  # noqa: BLE001
                backend = "memory(degraded)"
        detail = await self._fallback.describe()
        detail["backend"] = backend
        detail["degraded"] = self._degraded
        return detail


def build_store(settings: Settings) -> HybridStore:
    """按配置构造存储；未配置 ``REDIS_URL`` 直接用内存（本地/测试默认）。"""
    fallback = MemoryLRUStore(
        ttl_seconds=settings.idempotency_ttl_seconds,
        max_entries=settings.idempotency_max_entries,
    )
    if not settings.redis_url:
        logger.info("未配置 REDIS_URL，幂等键使用内存 LRU 存储")
        return HybridStore(primary=None, fallback=fallback, primary_expected=False)
    try:
        primary = RedisIdempotencyStore(
            url=settings.redis_url,
            ttl_seconds=settings.idempotency_ttl_seconds,
            prefix=settings.redis_key_prefix,
        )
    except ModuleNotFoundError:
        logger.warning("redis 包未安装，幂等键降级为内存 LRU")
        return HybridStore(primary=None, fallback=fallback, primary_expected=True)
    return HybridStore(primary=primary, fallback=fallback, primary_expected=True)


def _cacheable(status_code: int, headers: Mapping[str, str]) -> bool:
    """只缓存成功或确定性校验失败的 JSON；不缓存 5xx、401/403/409/429 等瞬态或冲突响应。"""
    content_type = headers.get("content-type", "")
    return (
        "application/json" in content_type.lower()
        and status_code < 500
        and status_code not in {401, 403, 409, 429}
    )


class IdempotencyMiddleware:
    """读取 ``Idempotency-Key`` 并实现「首次执行 + 重复复用」——**纯 ASGI 中间件**。

    为什么是纯 ASGI（不是 ``BaseHTTPMiddleware``）
    --------------------------------------------
    ``BaseHTTPMiddleware`` 把下游放进另一个 task 跑，导致租户上下文
    ``bind()`` / ``reset()`` 落在不同 context —— 这会让租户上下文错位，
    而错位的后果是**跨租户串数据**。纯 ASGI 形态下中间件与下游同一个 task，
    从根上消除这个错位。

    实现要点
    --------
    1. **``send`` 侧拦截**：看到 ``http.response.start`` 时抓 status 与响应头，
       据此当场判定「能不能缓存」（JSON 且 < 500）——不需要先把响应缓冲起来；
    2. 可缓存时**边转发边抄一份 body**，``more_body=False`` 时落库，
       用户感知的延迟不受影响；
    3. **异常路径**：下游抛异常时 ``release`` 掉占位、**不写入任何「成功」结果**，
       否则一次崩溃会被当成「首次执行的正确结果」重放出去；
    4. 租户命名空间：幂等键必须按租户隔离，否则 A 商户的
       ``export:stock:csv`` 会把 B 商户的导出结果直接重放回去。
       租户从 JWT claims 解出（**不查库**）；无 token / token 非法时不拒绝请求
       （那是鉴权层的事），退回 ``anon`` 命名空间。
    """

    def __init__(
        self,
        app: Any,  # noqa: ANN401 - Starlette 约定
        store: IdempotencyStore,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        self._store = store
        self._settings = settings

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http" or scope.get("method") not in IDEMPOTENT_METHODS:
            await self.app(scope, receive, send)
            return

        raw_key = _header_of(scope, b"idempotency-key")
        if raw_key is None or raw_key.strip() == "":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        key = f"{_tenant_namespace(scope, self._settings)}:{method}:{path}:{raw_key.strip()}"

        state = await self._store.begin(key)
        if state == STATE_IN_PROGRESS:
            spec = get_contracts().error_codes["E_DUPLICATE_REQUEST"]
            await JSONResponse(status_code=spec.http_status, content=failure(spec))(scope, receive, send)
            return
        if state == STATE_COMPLETED:
            cached = await self._store.get(key)
            if cached is not None:
                headers = dict(cached.headers)
                headers["X-Idempotent-Replay"] = "1"
                await Response(
                    content=cached.body,
                    status_code=cached.status_code,
                    media_type=cached.media_type,
                    headers=headers,
                )(scope, receive, send)
                return

        captured = _ResponseCapture()
        try:
            await self.app(scope, receive, captured.send_through(send))
        except Exception:
            # 下游炸了：释放占位，绝不把半成品当成「首次执行结果」缓存
            await self._store.release(key)
            raise

        snapshot = captured.snapshot()
        if snapshot is None:
            # 没抓到完整响应（不该发生）或不可缓存：释放占位，让下次能重试
            await self._store.release(key)
            return
        await self._store.complete(key, snapshot)


class _ResponseCapture:
    """转发的同时抄一份响应（只在可缓存时抄 body）。"""

    def __init__(self) -> None:
        self._status_code: int | None = None
        self._headers: dict[str, str] = {}
        self._body: list[bytes] = []
        self._cacheable = False
        self._completed = False

    def send_through(self, send: Any) -> Any:  # noqa: ANN401
        """返回一个「先转发再抄写」的 send。"""

        async def _send(message: Mapping[str, Any]) -> None:
            kind = message.get("type")
            if kind == "http.response.start":
                self._status_code = int(message.get("status", 200))
                self._headers = {
                    name.decode("latin-1"): value.decode("latin-1")
                    for name, value in message.get("headers", [])
                }
                self._cacheable = _cacheable(self._status_code, self._headers)
            elif kind == "http.response.body" and self._cacheable:
                body = message.get("body", b"")
                if body:
                    self._body.append(body if isinstance(body, bytes) else str(body).encode("utf-8"))
                if not message.get("more_body", False):
                    self._completed = True
            await send(message)

        return _send

    def snapshot(self) -> CachedResponse | None:
        """可缓存且已收尾时给出快照，否则 ``None``。"""
        if not self._cacheable or not self._completed or self._status_code is None:
            return None
        return CachedResponse(
            status_code=self._status_code,
            body=b"".join(self._body),
            media_type=self._headers.get("content-type", "application/json"),
            headers={
                name: value
                for name, value in self._headers.items()
                if name.lower() not in {"content-length", "transfer-encoding"}
            },
        )


def _header_of(scope: Mapping[str, Any], name: bytes) -> str | None:
    """从 ASGI scope 里取一个请求头的文本值。"""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


def _tenant_namespace(scope: Mapping[str, Any], settings: Settings | None) -> str:
    """幂等键的租户命名空间（**不解库**，只解 JWT claims）。

    拿不到就退回 ``anon``：登录接口本身还没有租户，而它的 key 是
    ``login:{code}``，code 一次性、天然唯一，不会撞。
    """
    authorization = _header_of(scope, b"authorization")
    if not authorization or not authorization.startswith("Bearer ") or settings is None:
        return "anon"
    try:
        from app.services.auth import decode_token  # 局部导入：避免模块级循环依赖

        claims = decode_token(settings, authorization[len("Bearer ") :].strip())
        return str(claims.get("tenant_id") or "anon")
    except Exception:  # noqa: BLE001 - 鉴权失败由 require_auth 负责报错，这里只降级
        return "anon"
