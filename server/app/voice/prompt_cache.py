"""prompt 前缀缓存（Q15）：把「每次都要重复的静态前缀」只算一次。

为什么值钱
----------
G4 的输入 90% 是静态内容（契约版本说明、38 个 intent 摘要、few-shot、Top20 商品）。
prompt 前缀缓存命中时，**输入 token 不再重复计费**，这是仅次于规则层命中的省钱手段；
同时它让「同一句话重复说」不必重复请求模型（60s 内的重复由 G2 拦截，更长的重复由本层接住）。

两级 key
--------
1. **前缀 key**：``prefix:{tenant}:{contract_version}:{domain}:{top_products_hash}``
   —— 只与静态内容有关，租户/契约变才失效；
2. **结果 key**：``result:{tenant}:{contract_version}:{domain}:{sha256(归一化原话)}``
   —— 命中即直接复用上次的 ``ParseResult``，一次模型都不调。

两者都带 TTL 与容量上限，纯内存实现（云托管不引入有状态服务，见 D7）；
Redis 可用时由 :class:`~app.tenant.cache.TenantCache` 换后端，接口不变。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "CacheEntry",
    "PromptCache",
    "normalize_for_cache",
    "result_key",
    "prefix_key",
]

#: 缓存条目默认 TTL（秒）；契约/商品变动应主动 ``invalidate_tenant``
DEFAULT_TTL_SECONDS = 3600
#: 容量上限（LRU 淘汰）
DEFAULT_MAX_ENTRIES = 256


def normalize_for_cache(text: str) -> str:
    """归一化原话（去空白与标点、统一大小写），让「同一句话」只占一个 key。"""
    cleaned = (text or "").strip().lower()
    for char in " \t\r\n，,。.、！!？?：:；;“”\"'（）()【】[]…—~·":
        cleaned = cleaned.replace(char, "")
    return cleaned


def _digest(text: str) -> str:
    """稳定短摘要（sha256 前 16 位足够避免碰撞，且日志不泄露原文长度特征）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def prefix_key(
    *,
    tenant_id: str,
    contract_version: str,
    domain: str | None,
    products: str = "",
) -> str:
    """静态前缀缓存 key。"""
    return "prefix:{}:{}:{}:{}".format(
        tenant_id or "-",
        contract_version or "-",
        domain or "-",
        _digest(products),
    )


def result_key(
    *,
    tenant_id: str,
    contract_version: str,
    domain: str | None,
    text: str,
    today_iso: str = "",
    member_id: str = "",
    dict_version: int = 0,
    context_hash: str = "",
) -> str:
    """解析结果缓存 key（含日期 / 成员 / 词表版本 / 上下文摘要，防止跨日或词表更新后串缓存）。"""
    return "result:{}:{}:{}:{}:{}:{}:{}:{}".format(
        tenant_id or "-",
        member_id or "-",
        contract_version or "-",
        today_iso or "-",
        int(dict_version or 0),
        domain or "-",
        context_hash or "-",
        _digest(normalize_for_cache(text)),
    )


@dataclass
class CacheEntry:
    """一条缓存（值 + 过期时间）。"""

    value: Any
    expires_at: float
    hits: int = 0

    def expired(self, now: float | None = None) -> bool:
        """是否已过期。"""
        return (now if now is not None else time.time()) >= self.expires_at


class PromptCache:
    """带 TTL + LRU 的进程内缓存（前缀与结果共用一套实现）。"""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = max(1, ttl_seconds)
        #: 插入顺序即 LRU 顺序（Python dict 保序）
        self._store: dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ 读写
    def get(self, key: str) -> Any | None:
        """取缓存；过期视作未命中并顺手清理。"""
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expired():
            self._store.pop(key, None)
            self.misses += 1
            return None
        entry.hits += 1
        self.hits += 1
        # 命中即刷新 LRU 位置
        self._store.pop(key, None)
        self._store[key] = entry
        return entry.value

    def set(self, key: str, value: Any) -> None:
        """写入缓存（超容量时淘汰最久未用）。"""
        self._store[key] = CacheEntry(value=value, expires_at=time.time() + self._ttl_seconds)
        while len(self._store) > self._max_entries:
            oldest = next(iter(self._store))
            self._store.pop(oldest, None)

    def delete(self, key: str) -> None:
        """删除单个 key（契约/商品变更时用）。"""
        self._store.pop(key, None)

    def invalidate_tenant(self, tenant_id: str) -> int:
        """清空某租户的全部缓存（商品/别名/契约变更后调用）。

        Returns:
            被删除的条目数。
        """
        needle = f":{tenant_id or '-'}:"
        doomed = [key for key in self._store if needle in key]
        for key in doomed:
            self._store.pop(key, None)
        return len(doomed)

    def clear(self) -> None:
        """清空并重置统计。"""
        self._store.clear()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ 统计
    @property
    def size(self) -> int:
        """当前条目数。"""
        return len(self._store)

    @property
    def hit_rate(self) -> float:
        """命中率（成本监控用）。"""
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def stats(self) -> Mapping[str, Any]:
        """拍平统计（``cost_monitor`` 读取）。"""
        return {
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl_seconds,
        }
