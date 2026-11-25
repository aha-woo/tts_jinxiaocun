"""``/readyz`` 就绪探针：DB 连通、表数、隔离能力自描述、JWT 密钥状态。

设计约定（T02）
--------------
- ``/healthz`` 是**存活探针**，T01 已冻结其结构，**不得**塞进 DB/RLS 字段；
- ``/readyz`` 是**就绪探针**，DB 与 RLS 状态只在这里出现；
- 非 PG 方言（本地 SQLite）**不是坏了**，而是设计内的「应用层隔离模式」：
  ``isolation_mode == "app_scope_only"``、``strength == "reduced_one_layer_less"``，
  如实标注「少一层 DB 强制隔离」，绝不声称 RLS 仍在生效。
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import EXPECTED_TABLES, Settings
from app.main import create_app

#: T01 冻结的 /healthz 顶层 data 字段（新增字段即视为破坏端侧契约）
FROZEN_HEALTHZ_KEYS = {
    "status",
    "version",
    "env",
    "checked_at",
    "contract",
    "idempotency",
    "capabilities",
}


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "env": "test",
        "database_url": "sqlite+pysqlite:///:memory:",
        "jwt_secret": "test-secret-for-t02-only-please-do-not-reuse",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    application = create_app(make_settings())
    application.state.database.create_all()
    with TestClient(application) as test_client:
        yield test_client


def test_readyz_reports_db_and_isolation(client: TestClient) -> None:
    """就绪探针：DB 可连、34 张表、隔离能力自描述齐全。"""
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0

    data = body["data"]
    assert data["database"]["reachable"] is True
    assert data["database"]["tables"] == EXPECTED_TABLES == 42
    assert data["database"]["tables_ok"] is True
    assert data["database"]["dialect"] == "sqlite"

    isolation = data["isolation"]
    assert isolation["rls_enabled"] is False, "SQLite 上不许声称 RLS 生效"
    assert isolation["isolation_mode"] == "app_scope_only"
    assert isolation["strength"] == "reduced_one_layer_less"
    assert "无 RLS" in isolation["notes"]


def test_readyz_is_also_under_api_v1(client: TestClient) -> None:
    """网关前缀兼容：/api/v1/readyz 与 /readyz 同源。"""
    primary = client.get("/readyz").json()["data"]["database"]
    prefixed = client.get("/api/v1/readyz").json()["data"]["database"]
    assert primary == prefixed


def test_readyz_flags_default_jwt_secret() -> None:
    """仍在用本地默认 JWT 密钥时如实标 degraded（生产必须换）。"""
    application = create_app(Settings(env="test", database_url="sqlite+pysqlite:///:memory:"))
    application.state.database.create_all()
    with TestClient(application) as test_client:
        data = test_client.get("/readyz").json()["data"]
    assert data["auth"]["jwt_secret_is_default"] is True
    assert "jwt_secret_is_default" in data["degraded_reasons"]
    assert data["status"] == "degraded"


def test_readyz_reports_missing_tables_as_not_ready() -> None:
    """没建表（或迁移没跑）时 tables_ok=False、HTTP 503 —— 别把空库当就绪。"""
    application = create_app(make_settings())
    with TestClient(application) as test_client:
        response = test_client.get("/readyz")
        assert application.state.database.ping() is True  # 连得上，但没表
    assert response.status_code == 503
    data = response.json()["data"]
    assert data["database"]["tables"] == 0
    assert data["database"]["tables_ok"] is False
    assert any("table_count_mismatch" in reason for reason in data["degraded_reasons"])


def test_healthz_structure_is_frozen(client: TestClient) -> None:
    """``/healthz`` 保持 T01 结构：DB/RLS 状态**不许**混进来。"""
    data = client.get("/healthz").json()["data"]
    assert set(data) == FROZEN_HEALTHZ_KEYS
    assert "database" not in data and "isolation" not in data


def test_healthz_still_reports_contract_counts(client: TestClient) -> None:
    """契约计数不受数据层改动影响（38/18/5/N/12）。"""
    contract = client.get("/healthz").json()["data"]["contract"]
    assert contract["intents"] == 38
    assert contract["slots"] == 18
    assert contract["sources"] == 5
    assert contract["domains"] == 12
    assert contract["error_codes"] == 25
