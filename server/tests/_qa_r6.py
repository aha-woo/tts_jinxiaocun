"""QA Round-2 追问探针 6（临时，不提交）：R0 只读问答是否真能返回数据。"""
from __future__ import annotations

import uuid

import app.models  # noqa: F401
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, UnitConversion
from app.models.inventory import Inventory
from app.utils.money import yuan_to_fen

TEST_SECRET = "qa-r6-secret-please-do-not-reuse"


def make_settings(**overrides):
    base = {"env": "test", "database_url": "sqlite+pysqlite:///:memory:", "jwt_secret": TEST_SECRET, "wx_provider": "fake", "llm_provider": "fake"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def app() -> FastAPI:
    application = create_app(make_settings())
    application.state.database.create_all()
    return application


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as c:
        yield c


def login(client, code):
    r = client.post("/api/v1/auth/login", json={"code": code})
    return r.json()["data"]


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def seed(app, tenant_id, store_id):
    pid = uuid.uuid4()
    with app.state.database.session(bypass_scope=True) as s:
        s.add(Product(tenant_id=uuid.UUID(tenant_id), product_id=pid, name="可乐", spec="500ml", base_unit="瓶", last_purchase_price_fen=3500, last_sale_price_fen=yuan_to_fen("3")))
        s.add(Inventory(tenant_id=uuid.UUID(tenant_id), store_id=uuid.UUID(store_id), product_id=pid, qty_base=192, version=0))
        s.add(UnitConversion(tenant_id=uuid.UUID(tenant_id), product_id=pid, from_unit="箱", to_unit="瓶", ratio=24, status="approved"))


def say(client, token, text):
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(token)).json()["data"]["session_id"]
    return client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": text, "asr_confidence": 0.95}, headers=hdr(token))


@pytest.mark.parametrize("text", ["今天卖了多少", "可乐还剩多少", "老张欠我多少", "昨天卖了多少"])
def test_readonly_answers(client: TestClient, app: FastAPI, text: str) -> None:
    a = login(client, f"r6-{abs(hash(text)) % 9999}")
    seed(app, a["tenant_id"], a["store_id"])
    r = say(client, a["jwt"], text)
    body = r.json()
    data = body.get("data") or {}
    print(f"\n[{text}] http={r.status_code} route={data.get('route')} intent={(data.get('parse_result') or {}).get('intent')} err={data.get('error_code')} code={body.get('code')}")
    print("  msg:", str(body.get("message"))[:120])
    ans = data.get("answer")
    print("  answer:", str(ans)[:220])
