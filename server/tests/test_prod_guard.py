"""生产配置自检：**错配就拒绝启动**（不能"服务能起来"但语义是错的）。

为什么这组断言必须存在
--------------------
三类错配的共同症状是「启动成功、接口 200」，但后果都是灾难：

1. ``DATABASE_URL`` 落到 SQLite → 没有 DB 级 RLS、重启丢数据；
2. ``WX_PROVIDER=fake`` → 任何 code 都能换出 openid，**任何人可开通租户**；
3. ``JWT_SECRET`` 默认值 → token 可伪造。

LLM / 支付已有同构保护（``llm_provider`` / ``pay/sign.py``），这里把数据层与登录补齐。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import create_app


def prod_settings(**overrides: object) -> Settings:
    """一个「配置正确」的生产 Settings，测试按需覆盖单项。"""
    base: dict[str, object] = {
        "env": "prod",
        "database_url": "postgresql+psycopg://vil:pwd@127.0.0.1:5432/vil",
        "jwt_secret": "x" * 48,
        "wx_provider": "real",
        "wx_appid": "wx-test",
        "wx_app_secret": "secret",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_prod_guard_allows_correct_config() -> None:
    """配置正确时不能误杀（否则大家会把自检注释掉）。"""
    app = create_app(prod_settings())
    assert app.state.settings.is_production() is True


def test_prod_guard_rejects_sqlite_database() -> None:
    """生产落 SQLite = 没 RLS + 重启丢数据，必须拒绝启动。"""
    with pytest.raises(RuntimeError, match="不是 PostgreSQL"):
        create_app(prod_settings(database_url="sqlite+pysqlite:///:memory:"))


def test_prod_guard_rejects_fake_login_provider() -> None:
    """fake 登录在生产等于「谁都能开通租户」。"""
    with pytest.raises(RuntimeError, match="WX_PROVIDER"):
        create_app(prod_settings(wx_provider="fake"))


def test_prod_guard_rejects_default_jwt_secret() -> None:
    """默认 JWT 密钥在生产等于「token 可伪造」。"""
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app(prod_settings(jwt_secret="local-dev-only-change-me"))


def test_local_env_is_not_blocked() -> None:
    """本地/测试仍然允许 SQLite（但要如实标注 isolation_mode）。"""
    app = create_app(Settings(env="local", database_url="sqlite+pysqlite:///:memory:"))
    assert app.state.database.capabilities()["isolation_mode"] == "app_scope_only"
