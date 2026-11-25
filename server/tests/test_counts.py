"""盘点：账面快照 → 填实盘 → 应用差异（写 count_adjust 流水、调余额与批次）。"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.inventory import Inventory, InventoryMovement, StockBatch


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-counts-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    application = create_app(_settings())
    database = application.state.database
    database.create_all()
    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-count-1"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        yield {
            "client": client,
            "database": database,
            "headers": {"Authorization": f"Bearer {data['jwt']}"},
            "tenant_id": uuid.UUID(data["tenant_id"]),
        }
    database.drop_all()
    database.dispose()


def _buy(client: TestClient, headers: dict[str, str], text: str) -> None:
    session = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    session_id = session.json()["data"]["session_id"]
    parsed = client.post(
        f"/api/v1/voice/sessions/{session_id}/parse",
        json={"asr_text": text, "asr_confidence": 0.95, "duration_ms": 2_000},
        headers=headers,
    )
    card_id = str(parsed.json()["data"]["card"]["meta"]["card_id"])
    confirmed = client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text


def test_count_adjusts_inventory_batch_and_writes_movement(shop: dict[str, object]) -> None:
    client = shop["client"]
    headers = shop["headers"]
    tenant_id = shop["tenant_id"]
    database = shop["database"]

    policy = client.patch(
        "/api/v1/inventory/policy",
        json={"cost_method": "batch"},
        headers=headers,
    )
    assert policy.status_code == 200, policy.text

    _buy(client, headers, "进了十五件可乐，一件八元")

    started = client.post("/api/v1/counts", headers=headers)
    assert started.status_code == 200, started.text
    count_id = started.json()["data"]["count_id"]

    detail = client.get(f"/api/v1/counts/{count_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    line = detail.json()["data"]["lines"][0]
    assert float(line["qty_book"]) == 15.0
    product_id = line["product_id"]

    filled = client.patch(
        f"/api/v1/counts/{count_id}/lines/{product_id}",
        json={"qty_counted": "10", "reason": "lost"},
        headers=headers,
    )
    assert filled.status_code == 200, filled.text

    applied = client.post(f"/api/v1/counts/{count_id}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["result"]["adjusted_lines"] == 1

    with database.session(bypass_scope=True) as session:
        inv = session.execute(
            select(Inventory).where(Inventory.tenant_id == tenant_id)
        ).scalar_one()
        assert float(inv.qty_base) == 10.0

        moves = list(
            session.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            ).scalars()
        )
        adjusts = [m for m in moves if m.reason == "count_adjust"]
        assert len(adjusts) == 1
        assert float(adjusts[0].qty_base) == -5.0

        batch = session.execute(
            select(StockBatch).where(StockBatch.tenant_id == tenant_id)
        ).scalar_one()
        assert float(batch.qty_left) == 10.0
