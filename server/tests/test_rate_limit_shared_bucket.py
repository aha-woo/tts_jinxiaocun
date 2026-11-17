"""限流必须**跨请求**生效（令牌桶不能每个请求发一个新的）。

缺陷原样（QA 实测）：``QuotaService.from_settings()`` 每次 ``MemoryQuotaStore()``，
于是每个请求都是空桶——连打 35 次 ``POST /voice/sessions`` 35 次全放行，
30/min 形同虚设。限流是**成本护栏**，不生效等于把供应商账单交给用户手速。

本文件钉两件事：
1. 同一个进程里，两次 ``from_settings()`` 必须落到**同一个**桶；
2. 第 N+1 次必须被拒（否则「共享」也只是个说法）。
"""

from __future__ import annotations

from app.config import Settings
from app.services.quota import (
    QuotaService,
    reset_shared_memory_stores,
    shared_memory_store,
)


def _settings(**overrides: object) -> Settings:
    """测试配置（内存 SQLite + 固定密钥 + 很小的桶，便于触发）。"""
    defaults: dict[str, object] = {
        "env": "test",
        "database_url": "sqlite+pysqlite:///:memory:",
        "jwt_secret": "test-secret-for-quota-only-do-not-reuse",
        "wx_provider": "fake",
        "quota_per_minute": 3,
        "quota_per_day": 50,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_two_services_share_the_same_bucket() -> None:
    """``from_settings()`` 调两次 ≠ 两个桶（这正是原来的缺陷）。"""
    reset_shared_memory_stores()
    settings = _settings()
    first = QuotaService.from_settings(settings)
    second = QuotaService.from_settings(settings)

    accepted = [first.take("tenant-x").allowed, second.take("tenant-x").allowed]
    assert accepted == [True, True], "第二次请求拿到的还是一个空桶"
    # 第三次刚好用完 3/min
    assert first.take("tenant-x").allowed is True
    # 第 4 次（**换一个新建的 service 实例**，模拟下一个请求）必须被拒
    rejected = QuotaService.from_settings(settings).take("tenant-x")
    assert rejected.allowed is False
    assert rejected.reason in {"rate_limited", "quota_exceeded"}
    assert rejected.error_code == "E_QUOTA_EXCEEDED"


def test_buckets_are_still_per_tenant() -> None:
    """共享桶不等于共享额度：不同租户各自计数。"""
    reset_shared_memory_stores()
    service = QuotaService.from_settings(_settings())

    for _ in range(3):
        assert service.take("tenant-a").allowed is True
    assert service.take("tenant-a").allowed is False
    assert service.take("tenant-b").allowed is True, "A 用完把 B 也一起限了"


def test_shared_store_is_keyed_by_limits() -> None:
    """桶参数不同 → 不同桶（改配置不该串到旧桶上）。"""
    reset_shared_memory_stores()
    from app.services.quota import QuotaLimits

    small = shared_memory_store(QuotaLimits(per_minute=1, per_day=10))
    large = shared_memory_store(QuotaLimits(per_minute=1, per_day=10))
    other = shared_memory_store(QuotaLimits(per_minute=2, per_day=10))
    assert small is large
    assert small is not other
