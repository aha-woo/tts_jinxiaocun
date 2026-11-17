"""QA Round-2 追问探针（临时，不提交）：定位 pending 守卫误伤主链路的范围。

运行： cd server && python -m pytest tests/_qa_r3.py -q -s
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
from app.utils.money import yuan_to_fen

TEST_SECRET = "qa-r3-secret-please-do-not-reuse"


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


def seed(app: FastAPI, tenant_id: str, store_id: str, *, with_ratio: bool = True) -> str:
    pid = uuid.uuid4()
    with app.state.database.session(bypass_scope=True) as s:
        s.add(Product(tenant_id=uuid.UUID(tenant_id), product_id=pid, name="可乐", spec="500ml", base_unit="瓶", last_purchase_price_fen=3500, last_sale_price_fen=yuan_to_fen("3")))
        s.add(Inventory(tenant_id=uuid.UUID(tenant_id), store_id=uuid.UUID(store_id), product_id=pid, qty_base=192, version=0))
        if with_ratio:
            s.add(UnitConversion(tenant_id=uuid.UUID(tenant_id), product_id=pid, from_unit="箱", to_unit="瓶", ratio=24, status="approved"))
    return str(pid)


def say(client: TestClient, token: str, text: str) -> dict:
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(token)).json()["data"]["session_id"]
    return client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": text, "asr_confidence": 0.95}, headers=hdr(token)).json()["data"]


def inv(app: FastAPI, tenant_id: str):  # noqa: ANN201
    with app.state.database.session(bypass_scope=True) as s:
        row = s.execute(select(Inventory).where(Inventory.tenant_id == uuid.UUID(tenant_id))).scalar_one_or_none()
        return None if row is None else float(row.qty_base)


def n_docs(app: FastAPI) -> int:
    with app.state.database.session(bypass_scope=True) as s:
        return int(s.execute(select(func.count()).select_from(DocHeader)).scalar_one())


def dump(tag: str, d: dict) -> dict:
    print(f"\n[{tag}] route={d.get('route')} intent={(d.get('parse_result') or {}).get('intent')} err={d.get('error_code')}")
    if d.get("parse_result"):
        print(f"  slots={d['parse_result']['slots']}")
        print(f"  source={d['parse_result']['source']}")
    if d.get("card"):
        print(f"  actions={[a['key'] for a in d['card']['actions']['actions']]}")
        print(f"  meta={d['card']['meta']}")
    return d


# ① 主链路原话（PRD 验收例 ⑦）
def test_prd_example_blocked(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r3-ex1")
    seed(app, a["tenant_id"], a["store_id"])
    d = dump("PRD例", say(client, a["jwt"], "进了两箱可乐，一件三十五"))
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("  confirm ->", r.status_code, r.text[:200])
    print("  docs:", n_docs(app), "inv:", inv(app, a["tenant_id"]))


# ② 补上供应商后是否可确认（edit 路径能不能救）
def test_edit_supplier_then_confirm(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r3-ex2")
    seed(app, a["tenant_id"], a["store_id"])
    d = say(client, a["jwt"], "进了两箱可乐，一件三十五")
    cid = d["card"]["meta"]["card_id"]
    e = client.patch(f"/api/v1/voice/cards/{cid}/edit", json={"slots": [{"code": "S", "value": "老张"}]}, headers=hdr(a["jwt"]))
    print("\n[edit] ->", e.status_code, e.text[:260])
    if e.status_code == 200:
        ed = e.json()["data"]
        print("  after edit actions:", [x["key"] for x in ed["card"]["actions"]["actions"]] if "card" in ed else list(ed))
        print("  after edit source:", (ed.get("parse_result") or {}).get("source"))
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("  confirm after edit ->", r.status_code, r.text[:200])
    print("  docs:", n_docs(app), "inv:", inv(app, a["tenant_id"]))


# ③ 原话里带供应商
def test_utterance_with_supplier(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r3-ex3")
    seed(app, a["tenant_id"], a["store_id"])
    d = dump("带供应商", say(client, a["jwt"], "从老张那进了两箱可乐，一件三十五"))
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("  confirm ->", r.status_code, r.text[:200])
    print("  docs:", n_docs(app), "inv:", inv(app, a["tenant_id"]))


# ④ 销售（C=散客 default，非 pending）
def test_sales_flow(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r3-ex4")
    seed(app, a["tenant_id"], a["store_id"])
    d = dump("销售", say(client, a["jwt"], "卖了三瓶可乐，一瓶三块"))
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("  confirm ->", r.status_code, r.text[:200])
    print("  docs:", n_docs(app), "inv:", inv(app, a["tenant_id"]))


# ⑤ 缺换算系数（P0-4 原始场景）——现在应被行级 pending 拦住
def test_missing_ratio_blocked(client: TestClient, app: FastAPI) -> None:
    a = login(client, "r3-ex5")
    seed(app, a["tenant_id"], a["store_id"], with_ratio=False)
    d = dump("缺系数", say(client, a["jwt"], "进了五箱可乐，一件三十五"))
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("  confirm ->", r.status_code, r.text[:240])
    print("  docs:", n_docs(app), "inv:", inv(app, a["tenant_id"]))
