"""租户级缓存 key 规范 —— 九道防线第 4 条。

铁律
----
- 缓存 key **只允许**通过 :func:`tenant_cache_key` / :class:`TenantCache` 拼装，
  格式固定 ``t:{tenant_id}:{store_id}:{biz}:{id}``（架构 §8.4 / §4.2）；
- 禁止裸 key（如 `card:{id}`），否则跨租户串数据；
- 缺少租户上下文直接抛错，不做「无前缀缓存」兜底。

后端当前为进程内 LRU（T01 幂等键同思路），T03 接 Redis 时只替换 ``CacheBackend``，
key 规则不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, MutableMapping, Protocol

from app.tenant.context import TenantContext, require


class CacheBackend(Protocol):
    """缓存后端协议（内存 / Redis 实现同一套语义）。"""

    def get(self, key: str) -> Any | None:
        """取未过期的值。"""

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """写入并设置 TTL。"""

    def delete(self, key: str) -> None:
        """删除单键。"""

    def delete_prefix(self, prefix: str) -> int:
        """按前缀批量删除，返回删除数量（销户/切租户用）。"""


@dataclass
class _Entry:
    value: Any
    expires_at: float


class MemoryCacheBackend:
    """进程内 TTL 缓存（本地/测试用；生产可换 Redis）。"""

    def __init__(self, max_entries: int = 4096) -> None:
        self._data: MutableMapping[str, _Entry] = {}
        self._max_entries = max_entries

    def _purge(self, now: float) -> None:
        expired = [key for key, entry in self._data.items() if entry.expires_at <= now]
        for key in expired:
            self._data.pop(key, None)

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            self._data.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        now = time.monotonic()
        self._purge(now)
        if len(self._data) >= self._max_entries:
            oldest = min(self._data, key=lambda item: self._data[item].expires_at)
            self._data.pop(oldest, None)
        self._data[key] = _Entry(value=value, expires_at=now + ttl_seconds)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        matched = [key for key in self._data if key.startswith(prefix)]
        for key in matched:
            self._data.pop(key, None)
        return len(matched)

    def size(self) -> int:
        """当前条目数（测试断言用）。"""
        return len(self._data)


def tenant_cache_key(
    tenant_id: Any,  # noqa: ANN401 - uuid.UUID / str
    store_id: Any,  # noqa: ANN401
    biz: str,
    identifier: Any,  # noqa: ANN401
) -> str:
    """拼装租户级缓存 key：``t:{tenant_id}:{store_id}:{biz}:{id}``。

    ``store_id`` 为空时用 ``-`` 占位，保证段数固定、便于前缀扫描。
    """
    store_part = str(store_id) if store_id else "-"
    return f"t:{tenant_id}:{store_part}:{biz}:{identifier}"


class TenantCache:
    """带租户前缀的缓存封装（key 拼装唯一入口）。"""

    def __init__(self, backend: CacheBackend | None = None) -> None:
        self._backend: CacheBackend = backend if backend is not None else MemoryCacheBackend()

    def key(
        self,
        biz: str,
        identifier: Any,  # noqa: ANN401
        *,
        store_id: Any | None = None,  # noqa: ANN401
        context: TenantContext | None = None,
    ) -> str:
        """生成 key；``store_id`` 缺省取上下文门店。"""
        ctx = context or require()
        return tenant_cache_key(ctx.tenant_id, store_id if store_id is not None else ctx.store_id, biz, identifier)

    def get(
        self,
        biz: str,
        identifier: Any,  # noqa: ANN401
        *,
        store_id: Any | None = None,  # noqa: ANN401
        context: TenantContext | None = None,
    ) -> Any | None:
        """读取缓存（自动加租户前缀）。"""
        return self._backend.get(self.key(biz, identifier, store_id=store_id, context=context))

    def set(
        self,
        biz: str,
        identifier: Any,  # noqa: ANN401
        value: Any,  # noqa: ANN401
        *,
        ttl_seconds: int = 60,
        store_id: Any | None = None,  # noqa: ANN401
        context: TenantContext | None = None,
    ) -> str:
        """写入缓存，返回实际 key（便于埋点）。"""
        key = self.key(biz, identifier, store_id=store_id, context=context)
        self._backend.set(key, value, ttl_seconds)
        return key

    def delete(
        self,
        biz: str,
        identifier: Any,  # noqa: ANN401
        *,
        store_id: Any | None = None,  # noqa: ANN401
        context: TenantContext | None = None,
    ) -> None:
        """删除单个缓存键。"""
        self._backend.delete(self.key(biz, identifier, store_id=store_id, context=context))

    def purge_tenant(self, tenant_id: Any) -> int:  # noqa: ANN401
        """清除某租户全部缓存（销户 / 词表击穿 / 权益变更时用）。"""
        return self._backend.delete_prefix(f"t:{tenant_id}:")

    @property
    def backend(self) -> CacheBackend:
        """暴露后端（测试/健康检查读取容量）。"""
        return self._backend


#: 进程级默认缓存实例（T03 换 Redis 时替换 backend 即可）
default_cache = TenantCache()
