"""QA Round-2 追问探针 4（临时，不提交）：实体意图卡片形态 + 撤销终态。

运行： cd server && python -m pytest tests/_qa_r4.py -q -s
"""

from __future__ import annotations

import uuid

import app.models  # noqa: F401
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, UnitConversion
from app.models.doc import DocHeader
from app.models.inventory import Inventory
from app.models.partner import Partner
from app.utils.money import yuan_to_fen

TEST_SECRET = "qa-r4-secret-please-do-not-reuse"


def make_settings(**overrides):  # noqa: ANN003, ANN201
    base = {
        "env": "test",
        "database_url": "sqlite+pysqlite:///:memory:",
        "jwt_secret": TEST_SECRET,
        "wx_provider": "fake",
        "llm_provider": "fake",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def app() -> FastAPI:
    application = create_app(make_settings())
    application.state.database.create_all()
    return application


@pytest.fixture()
def client(app: FastAPI):  # noqa: ANN201
    with TestClient(app) as c:
        yield c


def login(client: TestClient, code: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed(app: FastAPI, tenant_id: str, store_id: str) -> str:
    pid = uuid.uuid4()
    with app.state.database.session(bypass_scope=True) as s:
        s.add(Product(tenant_id=uuid.UUID(tenant_id), product_id=pid, name="可乐", spec="500ml", base_unit="瓶", last_purchase_price_fen=3500, last_sale_price_fen=yuan_to_fen("3")))
        s.add(Inventory(tenant_id=uuid.UUID(tenant_id), store_id=uuid.UUID(store_id), product_id=pid, qty_base=192, version=0))
        s.add(UnitConversion(tenant_id=uuid.UUID(tenant_id), from_unit="箱", to_unit="瓶", ratio=24, status="approved"))
    return str(pid)


def say(client: TestClient, token: str, text: str) -> dict:
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(token)).json()["data"]["session_id"]
    return client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": text, "asr_confidence": 0.95}, headers=hdr(token)).json()["data"]


def inv(app: FastAPI, tenant_id: str):  # noqa: ANN201
    with app.state.database.session(bypass_scope=True) as s:
        row = s.execute(select(Inventory).where(Inventory.tenant_id == uuid.UUID(tenant_id))).scalar_one_or_none()
        return None if row is None else float(row.qty_base)


def counts(app: FastAPI, tenant_id: str) -> dict:
    with app.state.database.session(bypass_scope=True) as s:
        return {
            "products": int(s.execute(select(func.count()).select_from(Product)).scalar_one()),
            "partners": int(s.execute(select(func.count()).select_from(Partner)).scalar_one()),
            "docs": int(s.execute(select(func.count()).select_from(DocHeader)).scalar_one()),
        }


# ① 实体意图的卡片长什么样？为什么 confirm 会被 pending_lines 拦？
def test_entity_card_shape(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r4-ent")
    seed(app, a["tenant_id"], a["store_id"])
    for text in ("新建商品雪碧", "加个供应商老王"):
        d = say(client, a["jwt"], text)
        print(f"\n[{text}] route={d.get('route')} intent={(d.get('parse_result') or {}).get('intent')} err={d.get('error_code')}")
        if not d.get("card"):
            continue
        pr = d["parse_result"]
        print("  slots:", pr.get("slots"))
        print("  source:", pr.get("source"))
        print("  lines:", pr.get("lines"))
        print("  impact:", d["card"].get("impact"))
        cid = d["card"]["meta"]["card_id"]
        r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
        print("  confirm ->", r.status_code, r.text[:220])
        print("  counts:", counts(app, a["tenant_id"]))


# ② 撤销终态（先通过 edit 补供应商，绕开 pending 误伤）
def test_undo_end_state_via_edit(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r4-undo")
    seed(app, a["tenant_id"], a["store_id"])
    d = say(client, a["jwt"], "进了两箱可乐，一件三十五")
    cid = d["card"]["meta"]["card_id"]
    client.patch(f"/api/v1/voice/cards/{cid}/edit", json={"slots": [{"code": "S", "value": "老张"}]}, headers=hdr(a["jwt"]))
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("\n进货 confirm ->", r.status_code, r.json()["data"]["result"]["doc_no"], "inv:", inv(app, a["tenant_id"]))
    purchase_no = r.json()["data"]["result"]["doc_no"]
    assert inv(app, a["tenant_id"]) == 240.0

    d2 = say(client, a["jwt"], "撤销上一单")
    pr2 = d2["parse_result"]
    print("undo route:", d2.get("route"), "intent:", pr2.get("intent"), "DOC:", pr2["slots"].get("DOC"), "source:", pr2.get("source"))
    cid2 = d2["card"]["meta"]["card_id"]
    r2 = client.post(f"/api/v1/voice/cards/{cid2}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("撤销 confirm ->", r2.status_code, r2.text[:300])
    print("撤销后 inv:", inv(app, a["tenant_id"]), "counts:", counts(app, a["tenant_id"]))
    with app.state.database.session(bypass_scope=True) as s:
        rows = s.execute(select(DocHeader.doc_no, DocHeader.intent_id, DocHeader.status, DocHeader.reversed_by, DocHeader.total_amount_fen)).all()
        print("单据:", [(x.doc_no, x.intent_id, x.status, str(x.reversed_by)[:8] if x.reversed_by else None, str(x.total_amount_fen)) for x in rows])
        orig = s.execute(select(DocHeader).where(DocHeader.doc_no == purchase_no)).scalar_one()
        print("原单 status:", orig.status, "reversed_by:", orig.reversed_by)
    print("=> 库存回退:", inv(app, a["tenant_id"]) == 192.0, "原单 reversed:", orig.status == "reversed")
