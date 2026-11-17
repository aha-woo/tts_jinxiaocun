"""租户上下文安全计数器（多租户最后一道闸的可观测面）。

为什么单独一个模块
------------------
「租户上下文跨 context 复位」这件事**不该只靠读注释确认安全**。
它必须是一个**能被看见的数字**：正常情况下恒为 0；一旦 > 0，说明有代码把
``bind()`` 与 ``reset()`` 拆到了**两个不同的 context 副本**里。已知有两种形态：

1. **同步生成器依赖 + 线程池**：Starlette 用 ``contextmanager_in_threadpool`` 跑
   同步 ``yield`` 依赖，``__enter__`` / ``__exit__`` 分处两个线程各自的 context 副本，
   在里面 ``bind()`` 拿的 token 到 ``__exit__`` 时必然失效
   （本仓库的实测来源：``Database.session(context=...)`` 的二次绑定，已改为幂等绑定）；
2. **``BaseHTTPMiddleware``**：把下游应用放进**另一个 task** 跑，中间件与下游的
   context 从此不是同一个。

两种形态都会让「租户上下文在请求结束时被复位」这条不变式悄悄失效，
而失效的后果不是报错，是**跨租户串数据**。所以这个计数器只有一件事：
让那条不变式从「推理」变成「读数」。

只依赖标准库，避免与 ORM / 配置形成循环导入。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "CONTEXT_RESET_CROSS_CONTEXT_TOTAL",
    "ContextSafetySnapshot",
    "context_safety_snapshot",
    "record_cross_context_reset",
    "reset_context_safety_counters",
]


class _Counter:
    """线程安全计数器（ASGI 同步/异步、线程池都可能并发调）。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._value = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        """指标名（与 ``infra/observability/metrics.yaml`` 对齐）。"""
        return self._name

    def increment(self, amount: int = 1) -> int:
        """自增并返回新值。"""
        with self._lock:
            self._value += amount
            return self._value

    def value(self) -> int:
        """当前值。"""
        with self._lock:
            return self._value

    def reset(self) -> None:
        """归零（**只给测试用**；生产不允许清零，清零等于擦掉证据）。"""
        with self._lock:
            self._value = 0


#: ``context_reset_cross_context_total`` —— 跨 context 复位被跳过的次数。
#:
#: 期望值：**恒为 0**。非 0 即代表 bind/reset 被拆到了两个 context 副本里
#: （同步生成器依赖 + 线程池，或 BaseHTTPMiddleware 的跨 task 形态），需要立刻回滚。
CONTEXT_RESET_CROSS_CONTEXT_TOTAL = _Counter("context_reset_cross_context_total")


@dataclass(frozen=True)
class ContextSafetySnapshot:
    """一次读数（可直接 JSON 化进 /readyz 或看板）。"""

    context_reset_cross_context_total: int

    @property
    def healthy(self) -> bool:
        """是否仍然满足「不得跨 context 复位」这条不变式。"""
        return self.context_reset_cross_context_total == 0

    def as_dict(self) -> Mapping[str, object]:
        """拍平。"""
        return {
            "context_reset_cross_context_total": self.context_reset_cross_context_total,
            "context_isolation_healthy": self.healthy,
        }


def record_cross_context_reset() -> int:
    """记一次跨 context 复位（由 :func:`app.tenant.context.reset` 调用）。"""
    return CONTEXT_RESET_CROSS_CONTEXT_TOTAL.increment()


def context_safety_snapshot() -> ContextSafetySnapshot:
    """取当前读数。"""
    return ContextSafetySnapshot(
        context_reset_cross_context_total=CONTEXT_RESET_CROSS_CONTEXT_TOTAL.value()
    )


def reset_context_safety_counters() -> None:
    """归零（**只给测试用**）。"""
    CONTEXT_RESET_CROSS_CONTEXT_TOTAL.reset()
