"""租户级语音额度闸门：令牌桶 **30/min + 1000/day**，Redis Lua 原子扣减，无 Redis 降级内存。

为什么要两层桶
--------------
- **分钟桶**挡「手抖连点 / 客户端重试风暴」，防止一分钟打爆成本；
- **日桶**挡「脚本刷」，对应 PRD 5.5 的租户日额度。

为什么必须原子
--------------
``GET → 判断 → DECR`` 三步在并发下会超发（两个请求同时看到 1 个余量）。
Redis 侧用 **单条 Lua 脚本**把「检查 + 扣减 + 设置过期」放进一个原子步骤；
无 Redis 时降级为进程内加锁的计数窗口（单实例够用，多实例不精确 —— 如实标注）。

与 ``usage_ledger`` 的分工
--------------------------
本模块只管**能不能再解析一次**（粗粒度、滑动窗口）；
真正的「计次」在 :mod:`app.services.usage_meter`，且**只有卡片成功生成**才写流水。
两者口径不同、互不替代：门禁是止损，流水是计费。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

__all__ = [
    "QuotaDecision",
    "QuotaLimits",
    "QuotaService",
    "QuotaStore",
    "MemoryQuotaStore",
    "RedisQuotaStore",
    "quota_key",
]

#: Redis key 前缀（租户 + 粒度，两段桶分开）
KEY_TEMPLATE = "vil:quota:{tenant_id}:{scope}:{bucket}"

#: 扣减脚本：一次往返完成「读余量 → 判断 → 扣减 → 设过期」，天然原子
_LUA_DECREMENT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if current > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  local used = tonumber(redis.call('GET', KEYS[1]))
  return {0, tonumber(ARGV[1]) - used}
end
return {1, tonumber(ARGV[1]) - current}
"""


def quota_key(tenant_id: str, scope: str, bucket: str) -> str:
    """构造桶 key（唯一拼装点，禁止散落字符串拼接）。"""
    return KEY_TEMPLATE.format(tenant_id=tenant_id or "-", scope=scope, bucket=bucket)


@dataclass(frozen=True)
class QuotaLimits:
    """两层桶的参数（产品假设值，需核实）。"""

    per_minute: int = 30
    per_day: int = 1000

    def limit_of(self, scope: str) -> int:
        """取某粒度的上限。"""
        return self.per_minute if scope == "min" else self.per_day

    def window_of(self, scope: str) -> int:
        """取某粒度的窗口秒数。"""
        return 60 if scope == "min" else 86_400


@dataclass
class QuotaDecision:
    """一次额度判定结果。"""

    allowed: bool
    scope: str
    limit: int
    remaining: int
    reason: str = "ok"
    degraded: bool = False
    error_code: str | None = None

    @property
    def exhausted(self) -> bool:
        """是否已耗尽。"""
        return not self.allowed and self.reason == "quota_exceeded"


class QuotaStore(Protocol):
    """桶存储协议（Redis / 内存两种实现）。"""

    def take(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """尝试扣 1 个；返回 ``(是否放行, 剩余)``。"""
        ...

    def peek(self, key: str, limit: int, *, now: float | None = None) -> int:
        """只看余量（不扣）。"""
        ...

    def reset(self, key: str) -> None:
        """重置（测试/人工补偿用）。"""
        ...


class MemoryQuotaStore:
    """进程内计数器（无 Redis 时的降级实现）。

    只在单实例内精确；多实例下会各算各的 —— 这是**已知限制**，因此
    ``QuotaService`` 会把它标记为 ``degraded=True`` 之外的正常实现（``degraded`` 仅
    表示「本该用 Redis 却退化了」，与 T01 ``/healthz`` 的语义保持一致）。
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def take(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """线程安全地扣 1（``now`` 可注入，便于测试固定窗口）。"""
        moment = now if now is not None else time.time()
        with self._lock:
            count, expires_at = self._counts.get(key, (0, moment + window_seconds))
            if moment >= expires_at:
                count, expires_at = 0, moment + window_seconds
            if count >= limit:
                self._counts[key] = (count, expires_at)
                return False, 0
            count += 1
            self._counts[key] = (count, expires_at)
            return True, max(0, limit - count)

    def peek(self, key: str, limit: int, *, now: float | None = None) -> int:
        """读剩余（``now`` 可注入）。"""
        moment = now if now is not None else time.time()
        with self._lock:
            count, expires_at = self._counts.get(key, (0, moment))
            if moment >= expires_at:
                return limit
            return max(0, limit - count)

    def reset(self, key: str) -> None:
        """删除计数。"""
        with self._lock:
            self._counts.pop(key, None)


class RedisQuotaStore:
    """Redis 实现：用 Lua 脚本保证「判断 + 扣减」原子。"""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script: Any = None
        try:  # pragma: no cover - 依赖真实 redis-py
            self._script = self._client.register_script(_LUA_DECREMENT)
        except Exception:  # noqa: BLE001 - 老版本客户端不支持 register_script 时退回 eval
            self._script = None

    def take(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """原子扣减（脚本返回 ``[allowed, remaining]``；``now`` 由 Redis TTL 承担）。"""
        del now  # 真 Redis 用 key 自带的 TTL，服务端时间由 Redis 负责
        if self._script is not None:  # pragma: no cover - 需真实 Redis
            allowed, remaining = self._script(keys=[key], args=[limit, window_seconds])
            return bool(int(allowed)), int(remaining)
        current = int(self._client.incr(key))  # pragma: no cover
        if current == 1:
            self._client.expire(key, window_seconds)
        if current > limit:
            self._client.decr(key)
            return False, 0
        return True, max(0, limit - current)

    def peek(self, key: str, limit: int, *, now: float | None = None) -> int:  # pragma: no cover
        """读剩余（Redis 由 TTL 保证窗口）。"""
        del now
        raw = self._client.get(key)
        used = int(raw) if raw is not None else 0
        return max(0, limit - used)

    def reset(self, key: str) -> None:  # pragma: no cover - 需真实 Redis
        """删除 key。"""
        self._client.delete(key)


def _now_buckets(now: float | None = None) -> tuple[str, str]:
    """当前分钟桶与日桶的标识（``YYYYMMDDHHMM`` / ``YYYYMMDD``）。"""
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime(now if now is not None else time.time()))
    return stamp, stamp[:8]


#: 进程内内存桶（**必须跨请求复用**）。
#:
#: 以前 ``from_settings()`` 每次都 ``MemoryQuotaStore()``，于是每个请求都是一个空桶：
#: 30/min 的限流形同虚设（实测连打 35 次 ``POST /voice/sessions`` 35 次全放行），
#: 而限流是**成本护栏**——不生效等于把供应商账单交给用户的手速。
#: 未配置 Redis 时按「桶参数」共用一个内存实现；配置了 Redis 就走 Redis（多实例一致）。
_MEMORY_STORES: dict[tuple[int, int], "MemoryQuotaStore"] = {}
_MEMORY_STORES_LOCK = threading.Lock()


def shared_memory_store(limits: "QuotaLimits") -> "MemoryQuotaStore":
    """取（或创建）该桶参数对应的进程内共享存储。

    Args:
        limits: 桶参数（分钟/日上限）；参数不同 → 不同桶，互不影响。

    Returns:
        进程内单例 :class:`MemoryQuotaStore`。
    """
    key = (int(limits.per_minute), int(limits.per_day))
    with _MEMORY_STORES_LOCK:
        store = _MEMORY_STORES.get(key)
        if store is None:
            store = MemoryQuotaStore()
            _MEMORY_STORES[key] = store
        return store


def reset_shared_memory_stores() -> None:
    """清空共享内存桶（**只给测试用**）。"""
    with _MEMORY_STORES_LOCK:
        _MEMORY_STORES.clear()


class QuotaService:
    """两层桶门禁：先查日桶再查分钟桶（任一超限即拒）。"""

    def __init__(
        self,
        store: QuotaStore | None = None,
        *,
        limits: QuotaLimits | None = None,
        degraded: bool = False,
        expected_redis: bool = False,
    ) -> None:
        """
        Args:
            store: 桶存储；``None`` 表示内存实现。
            limits: 桶参数。
            degraded: 是否处于降级（本该有 Redis 却没有）。
            expected_redis: 是否**本应**使用 Redis（决定 ``degraded`` 是否有意义）。
        """
        self._store = store or MemoryQuotaStore()
        self._limits = limits or QuotaLimits()
        self._degraded = degraded or (expected_redis and isinstance(self._store, MemoryQuotaStore))
        self._history: list[QuotaDecision] = []

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_settings(cls, settings: Any, redis_client: Any = None) -> "QuotaService":
        """按配置装配（有 Redis 用 Redis，没有就内存并标记降级）。"""
        limits = QuotaLimits(
            per_minute=int(getattr(settings, "quota_per_minute", 30)),
            per_day=int(getattr(settings, "quota_per_day", 1000)),
        )
        if redis_client is not None:
            return cls(RedisQuotaStore(redis_client), limits=limits, expected_redis=True)
        expected = bool(getattr(settings, "redis_url", ""))
        return cls(shared_memory_store(limits), limits=limits, expected_redis=expected)

    # ------------------------------------------------------------------ 判定
    def take(self, tenant_id: str, *, now: float | None = None) -> QuotaDecision:
        """尝试消耗 1 次语音解析额度。

        Args:
            tenant_id: 租户（桶按租户隔离）。
            now: 注入当前时间（测试固定窗口）。

        Returns:
            :class:`QuotaDecision`；``allowed=False`` 时映射 ``E_QUOTA_EXCEEDED``。
        """
        day_bucket, minute_bucket = _now_buckets(now)
        day_key = quota_key(tenant_id, "day", day_bucket)
        minute_key = quota_key(tenant_id, "min", minute_bucket)

        day_limit = self._limits.limit_of("day")
        day_ok, day_remaining = self._store.take(
            day_key, day_limit, self._limits.window_of("day"), now=now
        )
        if not day_ok:
            decision = QuotaDecision(
                allowed=False,
                scope="day",
                limit=day_limit,
                remaining=0,
                reason="quota_exceeded",
                degraded=self._degraded,
                error_code="E_QUOTA_EXCEEDED",
            )
            self._remember(decision)
            return decision

        minute_limit = self._limits.limit_of("min")
        minute_ok, minute_remaining = self._store.take(
            minute_key, minute_limit, self._limits.window_of("min"), now=now
        )
        if not minute_ok:
            decision = QuotaDecision(
                allowed=False,
                scope="min",
                limit=minute_limit,
                remaining=0,
                reason="rate_limited",
                degraded=self._degraded,
                error_code="E_QUOTA_EXCEEDED",
            )
            self._remember(decision)
            return decision

        decision = QuotaDecision(
            allowed=True,
            scope="min",
            limit=minute_limit,
            remaining=min(day_remaining, minute_remaining),
            reason="ok",
            degraded=self._degraded,
        )
        self._remember(decision)
        return decision

    def peek(self, tenant_id: str, *, now: float | None = None) -> Mapping[str, int]:
        """只看余量（前端展示「今天还能录几次」）。"""
        day_bucket, minute_bucket = _now_buckets(now)
        return {
            "day": self._store.peek(
                quota_key(tenant_id, "day", day_bucket), self._limits.limit_of("day"), now=now
            ),
            "minute": self._store.peek(
                quota_key(tenant_id, "min", minute_bucket), self._limits.limit_of("min"), now=now
            ),
        }

    def reset(self, tenant_id: str, *, now: float | None = None) -> None:
        """重置某租户当前窗口（人工补偿/测试用）。"""
        day_bucket, minute_bucket = _now_buckets(now)
        self._store.reset(quota_key(tenant_id, "day", day_bucket))
        self._store.reset(quota_key(tenant_id, "min", minute_bucket))

    # ------------------------------------------------------------------ 历史
    def history(self, *, limit: int = 50) -> tuple[QuotaDecision, ...]:
        """最近的判定历史（可查，降级排查用）。"""
        return tuple(self._history[-limit:])

    def _remember(self, decision: QuotaDecision) -> None:
        """记一行历史（最多保留 200 条，避免内存无界）。"""
        self._history.append(decision)
        if len(self._history) > 200:
            del self._history[: len(self._history) - 200]

    @property
    def limits(self) -> QuotaLimits:
        """桶参数。"""
        return self._limits

    @property
    def degraded(self) -> bool:
        """是否降级（本应 Redis 却用内存）。"""
        return self._degraded

    def new_request_id(self) -> str:
        """生成一个请求 id（便于排查「这一次为什么被拒」）。"""
        return uuid.uuid4().hex
