"""健康检查 + 包络 + 幂等 + 100K 守卫（T01 验收 ④ / fail-fast 验证）。

真实起 FastAPI app（TestClient 会跑 lifespan），因此这里同时验证了
「契约不合规就起不来」的 fail-fast 行为。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.common.envelope import SUCCESS_CODE
from app.common.errors import AppError
from app.common.idempotency import MemoryLRUStore
from app.config import ContractError, Settings, get_contracts
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """启动一个测试用 app（含 lifespan：契约校验 + 幂等存储初始化）。"""
    app = create_app(Settings(env="test"))
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_returns_envelope_with_contract_counts(client: TestClient) -> None:
    """GET /healthz：200 + code=0 + 契约计数 38/18/5/N/12。"""
    response = client.get("/healthz")
    assert response.status_code == 200

    body: dict[str, Any] = response.json()
    assert body["code"] == SUCCESS_CODE
    assert body["message"] == "ok"

    data = body["data"]
    assert data["status"] == "ok"
    assert data["version"] == Settings().version
    assert data["contract"]["intents"] == 38
    assert data["contract"]["slots"] == 18
    assert data["contract"]["sources"] == 5
    assert data["contract"]["domains"] == 12
    assert data["contract"]["error_codes"] == len(get_contracts().error_codes)
    assert data["contract"]["error_codes"] >= 20
    assert data["capabilities"]["max_request_body_bytes"] == 100 * 1024
    assert data["capabilities"]["web_client"] is False
    assert "电脑版微信大屏" in data["capabilities"]["memo"]


def test_healthz_alias_under_api_v1(client: TestClient) -> None:
    """/api/v1/healthz 与 /healthz 返回同一份契约计数（网关前缀兼容）。"""
    primary = client.get("/healthz").json()["data"]["contract"]
    prefixed = client.get("/api/v1/healthz").json()["data"]["contract"]
    assert primary == prefixed


def test_root_documents_no_web_client(client: TestClient) -> None:
    """根路径明确「无独立网页端」，避免误接入 H5。"""
    body = client.get("/").json()
    assert body["code"] == SUCCESS_CODE
    assert "无独立网页端" in body["data"]["memo"]


def test_app_error_maps_to_envelope() -> None:
    """AppError -> 包络（code=numeric，message=中文话术）。"""
    app = create_app(Settings(env="test"))

    @app.get("/_test/r3")
    async def _raise_r3() -> None:
        raise AppError("E_R3_PC_REQUIRED")

    with TestClient(app) as test_client:
        response = test_client.get("/_test/r3")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == 1206
    assert body["data"]["error_code"] == "E_R3_PC_REQUIRED"
    assert "电脑版微信大屏" in body["message"]


def test_unknown_path_returns_envelope(client: TestClient) -> None:
    """404 也必须是包络，不能是纯文本（端侧统一解包），且 HTTP 状态保留 404。"""
    response = client.get("/not-exist-path")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 1504
    assert body["data"]["error_code"] == "E_NOT_FOUND"
    assert "message" in body


def test_idempotency_reuses_first_result() -> None:
    """同一 Idempotency-Key 的第二次请求复用首次结果，业务只执行一次。"""
    app = create_app(Settings(env="test"))
    counter = {"value": 0}

    @app.post("/_test/write")
    async def _write() -> dict[str, Any]:
        counter["value"] += 1
        return {"code": 0, "data": {"n": counter["value"]}, "message": "ok"}

    with TestClient(app) as test_client:
        headers = {"Idempotency-Key": "card-abc-001"}
        first = test_client.post("/_test/write", headers=headers)
        second = test_client.post("/_test/write", headers=headers)
        third = test_client.post("/_test/write")

    assert counter["value"] == 2  # 前两次只执行一次，第三次无幂等键再执行一次
    assert first.json()["data"]["n"] == 1
    assert second.json()["data"]["n"] == 1
    assert second.headers.get("X-Idempotent-Replay") == "1"
    assert third.json()["data"]["n"] == 2


def test_request_body_over_100k_rejected(client: TestClient) -> None:
    """请求体 > 100K 直接 E_AUDIO_TOO_LARGE（音频只传引用，D11）。"""
    oversized = b"a" * (100 * 1024 + 1)
    response = client.post(
        "/_test/upload",
        content=oversized,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == 1404
    assert body["data"]["error_code"] == "E_AUDIO_TOO_LARGE"


def test_contract_fail_fast_on_missing_contracts(tmp_path: Path) -> None:
    """契约目录不合法时应用拒绝启动（fail-fast，不允许带病上线）。"""
    broken_settings = Settings(env="test", contracts_dir=tmp_path)
    with pytest.raises(ContractError):
        create_app(broken_settings)


@pytest.mark.asyncio()
async def test_memory_store_begin_complete_release() -> None:
    """内存幂等存储状态机：new -> in_progress -> completed / release。"""
    store = MemoryLRUStore(ttl_seconds=60, max_entries=8)
    assert await store.begin("k1") == "new"
    assert await store.begin("k1") == "in_progress"
    assert await store.get("k1") is None

    from app.common.idempotency import CachedResponse

    cached = CachedResponse(
        status_code=200,
        body=b'{"code":0,"data":{"n":1},"message":"ok"}',
        media_type="application/json",
        headers={"content-type": "application/json"},
    )
    await store.complete("k1", cached)
    assert await store.begin("k1") == "completed"
    assert await store.get("k1") == cached

    await store.begin("k2")
    await store.release("k2")
    assert await store.begin("k2") == "new"
    described = await store.describe()
    assert described["backend"] == "memory"


def test_shared_envelope_typescript_exists() -> None:
    """端侧包络契约文件存在（29 个错误码常量与解包逻辑在 api.ts 消费）。"""
    envelope_ts = REPO_ROOT / "packages" / "shared" / "contracts" / "api-envelope.ts"
    assert envelope_ts.is_file()
    content = envelope_ts.read_text(encoding="utf-8")
    assert "SUCCESS_CODE = 0" in content
    assert "export function unwrap" in content
