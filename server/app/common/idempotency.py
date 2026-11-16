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
from typing import Any, Protocol

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

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

        self._client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
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


def _cacheable(response: Response) -> bool:
    """只缓存成功/业务失败的 JSON 响应；5xx 与二进制不缓存。"""
    content_type = response.headers.get("content-type", "")
    return "application/json" in content_type.lower() and response.status_code < 500


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """读取 ``Idempotency-Key`` 并实现「首次执行 + 重复复用」。"""

    def __init__(self, app: Any, store: HybridStore) -> None:  # noqa: ANN401 - Starlette 约定
        super().__init__(app)
        self._store = store

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in IDEMPOTENT_METHODS:
            return await call_next(request)

        raw_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
        if raw_key is None or raw_key.strip() == "":
            return await call_next(request)

        # 租户维度命名空间：T02 会在鉴权中间件里写入 request.state.tenant_id
        tenant = getattr(request.state, "tenant_id", "anon")
        key = f"{tenant}:{raw_key.strip()}"

        state = await self._store.begin(key)
        if state == STATE_IN_PROGRESS:
            spec = get_contracts().error_codes["E_DUPLICATE_REQUEST"]
            return JSONResponse(status_code=spec.http_status, content=failure(spec))
        if state == STATE_COMPLETED:
            cached = await self._store.get(key)
            if cached is not None:
                headers = dict(cached.headers)
                headers["X-Idempotent-Replay"] = "1"
                return Response(
                    content=cached.body,
                    status_code=cached.status_code,
                    media_type=cached.media_type,
                    headers=headers,
                )

        response = await call_next(request)

        if not _cacheable(response):
            await self._store.release(key)
            return response

        body = await self._read_body(response)
        snapshot = CachedResponse(
            status_code=response.status_code,
            body=body,
            media_type=response.headers.get("content-type", "application/json"),
            headers={
                key_name: value
                for key_name, value in response.headers.items()
                if key_name.lower() not in {"content-length", "transfer-encoding"}
            },
        )
        await self._store.complete(key, snapshot)
        return Response(
            content=snapshot.body,
            status_code=snapshot.status_code,
            media_type=snapshot.media_type,
            headers=snapshot.headers,
        )

    @staticmethod
    async def _read_body(response: Response) -> bytes:
        """把（流式）响应体完整读出来再回写，便于缓存。"""
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            body = getattr(response, "body", b"")
            return body if isinstance(body, bytes) else str(body).encode("utf-8")
        chunks: list[bytes] = []
        async for chunk in body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
        return b"".join(chunks)
