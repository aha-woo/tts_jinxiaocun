"""商品档案待补：缺进价能被列出来，补价后从清单消失。"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.doc import DocHeader, DocLine
from app.models.inventory import Inventory, InventoryMovement
from app.models.partner import Partner


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
        store_id = uuid.UUID(data["store_id"])
        member_id = uuid.UUID(data["member_id"])
        cheap = uuid.uuid4()
        priced = uuid.uuid4()
        with database.session(bypass_scope=True) as session:
            session.add(Product(tenant_id=tenant_id, product_id=cheap, name="缺价货", base_unit="件"))
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=priced,
                    name="有价货",
                    spec="500ml",
                    base_unit="瓶",
                    last_purchase_price_fen=800,
                    last_sale_price_fen=1500,
                    avg_cost_fen=800,
                    low_stock_qty=Decimal("12"),
                )
            )
            session.add(
                Inventory(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=priced,
                    qty_base=Decimal("24"),
                    avg_cost_fen=800,
                )
            )
            session.add(
                ProductAlias(
                    tenant_id=tenant_id,
                    product_id=priced,
                    member_id=None,
                    alias_text="大瓶水",
                    source="personal",
                )
            )
            session.add(
                UnitConversion(
                    tenant_id=tenant_id,
                    product_id=priced,
                    from_unit="箱",
                    to_unit="瓶",
                    ratio=Decimal("24"),
                    status="approved",
                )
            )
            session.add(
                InventoryMovement(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=priced,
                    reason="purchase",
                    qty_base=Decimal("24"),
                    unit_cost_fen=800,
                    created_by=member_id,
                )
            )
            partner_id = uuid.uuid4()
            doc_id = uuid.uuid4()
            session.add(
                Partner(
                    tenant_id=tenant_id,
                    partner_id=partner_id,
                    name="张老板",
                    type="customer",
                )
            )
            session.add(
                DocHeader(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    doc_id=doc_id,
                    doc_no="XS20261127001",
                    intent_id="sales.create",
                    doc_type="sales",
                    partner_id=partner_id,
                    biz_date=date(2026, 11, 27),
                    payment_status="已结",
                    total_amount_fen=36000,
                    created_by=member_id,
                )
            )
            session.add(
                DocLine(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    doc_id=doc_id,
                    product_id=priced,
                    qty=Decimal("24"),
                    unit="瓶",
                    qty_base=Decimal("24"),
                    unit_price_fen=1500,
                    amount_fen=36000,
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


def test_product_detail_and_stock_fields(shop: dict[str, object]) -> None:
    client = shop["client"]
    resp = client.get(f"/api/v1/products/{shop['priced']}", headers=shop["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["product"]["name"] == "有价货"
    assert data["product"]["low_stock_qty"] == "12.0000"
    assert data["inventory"]["qty_display"] == "24瓶"
    assert data["inventory"]["stock_value_fen"] == 19200
    assert data["aliases"] == ["大瓶水"]
    assert data["unit_conversions"] == [{"from_unit": "箱", "to_unit": "瓶", "ratio": "24"}]
    assert len(data["movements"]) == 1
    assert data["movements"][0]["reason"] == "purchase"
    assert data["movements"][0]["unit_cost_fen"] == 800

    missing = client.get(f"/api/v1/products/{uuid.uuid4()}", headers=shop["headers"])
    assert missing.status_code == 422
    assert missing.json()["code"] != 0

    stock = client.get("/api/v1/query/stock?limit=100", headers=shop["headers"])
    assert stock.status_code == 200, stock.text
    items = stock.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["low_stock_qty"] == "12.0000"

    docs = client.get("/api/v1/query/docs", headers=shop["headers"])
    assert docs.status_code == 200, docs.text
    doc_items = docs.json()["data"]["items"]
    assert len(doc_items) == 1
    assert doc_items[0]["summary"] == "有价货 500ml × 24瓶"
    assert doc_items[0]["partner_name"] == "张老板"
