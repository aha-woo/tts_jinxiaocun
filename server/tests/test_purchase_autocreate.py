"""进货时商品没有档案：确认时自动建档，库存按数量落账（不再要求先去建档）。"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product
from app.models.inventory import Inventory, InventoryMovement

UTTERANCE = "进了十五件可乐，一件八元"


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-autocreate-product-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    application = create_app(_settings())
    database = application.state.database
    database.create_all()

    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-autocreate-1"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        tenant_id = uuid.UUID(data["tenant_id"])
        # 注意：这里**故意不**预置商品档案
        yield {"client": client, "database": database, "headers": headers, "tenant_id": tenant_id}

    database.drop_all()
    database.dispose()


def _say(client: TestClient, headers: dict[str, str], text: str) -> dict:
    session = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    assert session.status_code == 200, session.text
    session_id = session.json()["data"]["session_id"]
    parsed = client.post(
        f"/api/v1/voice/sessions/{session_id}/parse",
        json={"asr_text": text, "asr_confidence": 0.95, "duration_ms": 2_000},
        headers=headers,
    )
    assert parsed.status_code == 200, parsed.text
    return parsed.json()["data"]


def test_purchase_unknown_product_creates_catalog_on_confirm(shop: dict[str, object]) -> None:
    client = shop["client"]
    headers = shop["headers"]
    tenant_id = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, UTTERANCE)
    assert data["route"] == "card", data
    card = data["card"]
    card_id = str(card["meta"]["card_id"])

    confirmed = client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()["data"]["result"]
    assert result["confirmed"] is True, result

    with database.session(bypass_scope=True) as session:
        products = list(
            session.execute(select(Product).where(Product.tenant_id == tenant_id)).scalars()
        )
        assert [p.name for p in products] == ["可乐"]
        product = products[0]
        assert product.base_unit == "件"
        assert product.last_purchase_price_fen == 800
        rows = list(
            session.execute(select(Inventory).where(Inventory.tenant_id == tenant_id)).scalars()
        )
        assert rows, "库存没有落账"
        assert sum(float(row.qty_base) for row in rows) == 15.0

        moves = list(
            session.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            ).scalars()
        )
        assert [m.reason for m in moves] == ["purchase"]
        assert sum(float(m.qty_base) for m in moves) == 15.0
        assert int(moves[0].unit_cost_fen or 0) == 800


def test_reversal_writes_negative_movement(shop: dict[str, object]) -> None:
    client = shop["client"]
    headers = shop["headers"]
    tenant_id = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, UTTERANCE)
    card_id = str(data["card"]["meta"]["card_id"])
    confirmed = client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    doc_id = confirmed.json()["data"]["result"]["doc_id"]

    reversed_resp = client.post(
        f"/api/v1/docs/{doc_id}/reversal",
        json={"reason": "测试撤销"},
        headers=headers,
    )
    assert reversed_resp.status_code == 200, reversed_resp.text

    with database.session(bypass_scope=True) as session:
        moves = list(
            session.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            ).scalars()
        )
        assert sorted(m.reason for m in moves) == ["purchase", "purchase"]
        assert sum(float(m.qty_base) for m in moves) == 0.0
