"""批次与成本法：两次不同进价，卖货按 FIFO 扣批次并算出销售成本。"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.doc import DocHeader
from app.models.inventory import StockBatch

P1 = "进了十五件可乐，一件八元"
P2 = "进了十五件可乐，一件十元"
SELL = "卖了二十件可乐，一件十二元"


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-batches-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    application = create_app(_settings())
    database = application.state.database
    database.create_all()
    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-batch-1"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        yield {
            "client": client,
            "database": database,
            "headers": headers,
            "tenant_id": uuid.UUID(data["tenant_id"]),
        }
    database.drop_all()
    database.dispose()


def _say(client: TestClient, headers: dict[str, str], text: str) -> dict:
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
    assert parsed.status_code == 200, parsed.text
    return parsed.json()["data"]


def _confirm(client: TestClient, headers: dict[str, str], data: dict) -> dict:
    card_id = str(data["card"]["meta"]["card_id"])
    resp = client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]["result"]
    assert result["confirmed"] is True, result
    return result


def _buy(client: TestClient, headers: dict[str, str], text: str) -> dict:
    return _confirm(client, headers, _say(client, headers, text))


def test_fifo_consumes_oldest_batch_and_sets_cogs(shop: dict[str, object]) -> None:
    client = shop["client"]
    headers = shop["headers"]
    tenant_id = shop["tenant_id"]
    database = shop["database"]

    policy = client.patch(
        "/api/v1/inventory/policy",
        json={"cost_method": "fifo", "allow_negative": False},
        headers=headers,
    )
    assert policy.status_code == 200, policy.text

    _buy(client, headers, P1)   # 15 件 × 8 元
    _buy(client, headers, P2)   # 15 件 × 10 元
    result = _buy(client, headers, SELL)  # 卖 20 件，先扣 8 元那批

    with database.session(bypass_scope=True) as session:
        batches = list(
            session.execute(
                select(StockBatch)
                .where(StockBatch.tenant_id == tenant_id)
                .order_by(StockBatch.unit_cost_fen)
            ).scalars()
        )
        assert len(batches) == 2
        cheap = next(b for b in batches if int(b.unit_cost_fen or 0) == 800)
        dear = next(b for b in batches if int(b.unit_cost_fen or 0) == 1000)
        assert float(cheap.qty_left) == 0.0
        assert float(dear.qty_left) == 10.0

        doc = session.execute(
            select(DocHeader).where(DocHeader.doc_id == uuid.UUID(str(result["doc_id"])))
        ).scalar_one()
        # 15×800 + 5×1000 = 17000 分
        assert int(doc.cogs_fen or 0) == 17000


def test_default_moving_avg_sets_weighted_cogs(shop: dict[str, object]) -> None:
    """默认移动加权平均：15×8元 + 15×10元 → 均价 9 元，卖 20 件成本 18000 分，不强制建批。"""
    client = shop["client"]
    headers = shop["headers"]
    tenant_id = shop["tenant_id"]
    database = shop["database"]

    _buy(client, headers, P1)
    _buy(client, headers, P2)
    result = _buy(client, headers, SELL)

    with database.session(bypass_scope=True) as session:
        batches = list(
            session.execute(select(StockBatch).where(StockBatch.tenant_id == tenant_id)).scalars()
        )
        assert batches == [], "默认未开批次的商品不应创建物理批次"
        doc = session.execute(
            select(DocHeader).where(DocHeader.doc_id == uuid.UUID(str(result["doc_id"])))
        ).scalar_one()
        assert int(doc.cogs_fen or 0) == 18000


def test_sale_over_stock_is_rejected(shop: dict[str, object]) -> None:
    client = shop["client"]
    headers = shop["headers"]

    policy = client.patch(
        "/api/v1/inventory/policy",
        json={"allow_negative": False},
        headers=headers,
    )
    assert policy.status_code == 200, policy.text

    _buy(client, headers, P1)  # 只有 15 件
    data = _say(client, headers, "卖了二十件可乐，一件十二元")
    card_id = str(data["card"]["meta"]["card_id"])
    resp = client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert resp.status_code != 200
    body = resp.json()
    assert "库存不足" in str(body), body
