"""商品档案待补：缺进价能被列出来，补价后从清单消失。"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-product-gaps-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    application = create_app(_settings())
    database = application.state.database
    database.create_all()
    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-gaps-1"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        tenant_id = uuid.UUID(data["tenant_id"])
        cheap = uuid.uuid4()
        priced = uuid.uuid4()
        with database.session(bypass_scope=True) as session:
            session.add(Product(tenant_id=tenant_id, product_id=cheap, name="缺价货", base_unit="件"))
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=priced,
                    name="有价货",
                    base_unit="件",
                    last_purchase_price_fen=800,
                )
            )
        yield {
            "client": client,
            "headers": headers,
            "cheap": cheap,
            "priced": priced,
        }
    database.drop_all()
    database.dispose()


def test_gaps_lists_only_missing_cost(shop: dict[str, object]) -> None:
    client = shop["client"]
    resp = client.get("/api/v1/products/gaps", headers=shop["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert [item["name"] for item in items] == ["缺价货"]


def test_patch_price_removes_from_gaps(shop: dict[str, object]) -> None:
    client = shop["client"]
    patched = client.patch(
        f"/api/v1/products/{shop['cheap']}",
        json={"last_purchase_price_fen": 800},
        headers=shop["headers"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["product"]["last_purchase_price_fen"] == 800
    resp = client.get("/api/v1/products/gaps", headers=shop["headers"])
    assert resp.json()["data"]["items"] == []
