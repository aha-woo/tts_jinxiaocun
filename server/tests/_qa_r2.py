"""QA Round-2 复验探针（临时，不提交）：针对 31b2e3a 的修复。

运行： cd server && python -m pytest tests/_qa_r2.py -q -s
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
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.doc import ArAp, DocHeader
from app.models.inventory import Inventory
from app.models.partner import Partner
from app.models.quota import UsageLedger
from app.models.voice import ConfirmCard
from app.models.dict import PersonalDict
from app.utils.money import yuan_to_fen

TEST_SECRET = "qa-r2-secret-please-do-not-reuse"


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


def count(app: FastAPI, model) -> int:  # noqa: ANN001
    with app.state.database.session(bypass_scope=True) as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar_one())


def seed_product(app: FastAPI, tenant_id: str, store_id: str, price_fen: int = 180) -> str:
    pid = uuid.uuid4()
    with app.state.database.session(bypass_scope=True) as s:
        s.add(Product(tenant_id=uuid.UUID(tenant_id), product_id=pid, name="可乐", spec="500ml", base_unit="瓶", last_purchase_price_fen=price_fen, last_sale_price_fen=yuan_to_fen("3")))
        s.add(Inventory(tenant_id=uuid.UUID(tenant_id), store_id=uuid.UUID(store_id), product_id=pid, qty_base=192, version=0))
        s.add(UnitConversion(tenant_id=uuid.UUID(tenant_id), product_id=pid, from_unit="箱", to_unit="瓶", ratio=24, status="approved"))
    return str(pid)


def parse_card(client: TestClient, app: FastAPI, token: str, text: str) -> dict:
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(token)).json()["data"]["session_id"]
    d = client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": text, "asr_confidence": 0.95}, headers=hdr(token)).json()["data"]
    return d


# ============================================================ 关键回归：主链路还能不能确认
def test_happy_path_purchase_still_confirmable(client: TestClient, app: FastAPI) -> None:
    """「进了两箱可乐」默认 S=待补（source=pending）——pending 守卫会不会把主链路一起拦死？"""
    a = login(client, "code-happy")
    seed_product(app, a["tenant_id"], a["store_id"])
    d = parse_card(client, app, a["jwt"], "进了两箱可乐，一件三十五")
    print("\nroute:", d.get("route"))
    print("slots.source:", d["parse_result"]["source"])
    print("card actions:", [x["key"] for x in d["card"]["actions"]["actions"]])
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("confirm ->", r.status_code, r.text[:400])
    print("docs:", count(app, DocHeader), "inventory:", _inv(app, a["tenant_id"]))
    assert r.status_code == 200, "主链路被 pending 守卫拦死了"


def _inv(app: FastAPI, tenant_id: str):  # noqa: ANN201
    with app.state.database.session(bypass_scope=True) as s:
        row = s.execute(select(Inventory).where(Inventory.tenant_id == uuid.UUID(tenant_id))).scalar_one_or_none()
        return None if row is None else row.qty_base


# ============================================================ P0-1 撤销终态
def test_sys_undo_end_state(client: TestClient, app: FastAPI) -> None:
    a = login(client, "code-undo")
    seed_product(app, a["tenant_id"], a["store_id"])
    d = parse_card(client, app, a["jwt"], "进了两箱可乐，一件三十五")
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("\n进货 confirm:", r.status_code, r.json()["data"]["result"]["doc_no"], "inv", _inv(app, a["tenant_id"]))
    purchase_no = r.json()["data"]["result"]["doc_no"]
    assert _inv(app, a["tenant_id"]) == 240

    d2 = parse_card(client, app, a["jwt"], "撤销上一单")
    print("sys.undo route:", d2.get("route"), "intent:", d2["parse_result"]["intent"], "doc slot:", d2["parse_result"]["slots"].get("DOC"), "source:", d2["parse_result"]["source"])
    cid2 = d2["card"]["meta"]["card_id"]
    r2 = client.post(f"/api/v1/voice/cards/{cid2}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    res = r2.json()["data"]["result"]
    print("撤销 confirm:", r2.status_code, "doc_no:", res["doc_no"], "reversal:", res["reversal_doc_no"], "detail:", res["detail"][:80])
    print("撤销后 inv:", _inv(app, a["tenant_id"]))
    with app.state.database.session(bypass_scope=True) as s:
        rows = s.execute(select(DocHeader.doc_no, DocHeader.intent_id, DocHeader.status, DocHeader.total_amount_fen)).all()
        print("单据:", [(x.doc_no, x.intent_id, x.status, str(x.total_amount_fen)) for x in rows])
        orig = s.execute(select(DocHeader).where(DocHeader.doc_no == purchase_no)).scalar_one()
        print("原单 status:", orig.status, "reversed_by:", orig.reversed_by)
    assert _inv(app, a["tenant_id"]) == 192, "库存没回退"
    assert orig.status == "reversed" and orig.reversed_by is not None


# ============================================================ P0-2 八个写意图终态
@pytest.mark.parametrize(
    ("text", "check"),
    [
        ("新建商品雪碧", "product"),
        ("可乐进价改成三十八", "price"),
    ],
)
def test_entity_intents_http(client: TestClient, app: FastAPI, text: str, check: str) -> None:
    a = login(client, f"code-entity-{check}")
    seed_product(app, a["tenant_id"], a["store_id"])
    d = parse_card(client, app, a["jwt"], text)
    print(f"\n{text} -> route={d.get('route')} err={d.get('error_code')} intent={ (d.get('parse_result') or {}).get('intent')}")
    if d.get("route") != "card":
        pytest.skip("模板/LLM 没命中该意图（不是本轮修复范围）")
    cid = d["card"]["meta"]["card_id"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("confirm ->", r.status_code, r.text[:300])
    with app.state.database.session(bypass_scope=True) as s:
        products = s.execute(select(Product)).scalars().all()
        print("products:", [(p.name, p.last_purchase_price_fen) for p in products])


# ============================================================ P1-12 复活 card_id
def test_revive_card_id_matches(client: TestClient, app: FastAPI) -> None:
    a = login(client, "code-revive2")
    seed_product(app, a["tenant_id"], a["store_id"])
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(a["jwt"])).json()["data"]["session_id"]
    d1 = client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": "进了两箱可乐，一件三十五", "asr_confidence": 0.95}, headers=hdr(a["jwt"])).json()["data"]
    d2 = client.post(f"/api/v1/voice/sessions/{s}/parse", json={"asr_text": "进了两箱可乐，一件三十五", "asr_confidence": 0.95}, headers=hdr(a["jwt"])).json()["data"]
    id1, id2 = d1["card"]["meta"]["card_id"], d2["card"]["meta"]["card_id"]
    print("\nid1:", id1, "\nid2:", id2, "\nsame:", id1 == id2, "billed2:", d2.get("credit_consumed"))
    assert id1 == id2
    r = client.post(f"/api/v1/voice/cards/{id2}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print("用 id2 确认 ->", r.status_code, r.json()["data"]["result"]["doc_no"])
    assert r.status_code == 200


# ============================================================ P1-5 限流
def test_rate_limit_now_effective(client: TestClient, app: FastAPI) -> None:
    a = login(client, "code-rate2")
    allowed = rejected = 0
    for _ in range(35):
        r = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(a["jwt"]))
        data = r.json().get("data") or {}
        if data.get("error_code") == "E_QUOTA_EXCEEDED" or r.status_code == 429:
            rejected += 1
        elif data.get("session_id"):
            allowed += 1
    print(f"\n35 次: allowed={allowed} rejected={rejected}")
    b = login(client, "code-rate2-b")
    rb = client.post("/api/v1/voice/sessions", json={"duration_ms": 2000, "energy_rms": 0.5}, headers=hdr(b["jwt"]))
    print("另一租户 ->", rb.status_code, (rb.json().get("data") or {}).get("session_id") is not None)
    assert rejected >= 5, "限流仍失效"


# ============================================================ pending 拦截（行级 + 槽位级）
def test_pending_slot_blocks(client: TestClient, app: FastAPI) -> None:
    a = login(client, "code-pending-slot")
    seed_product(app, a["tenant_id"], a["store_id"])
    d = parse_card(client, app, a["jwt"], "进了两箱可乐，一件三十五")
    cid = d["card"]["meta"]["card_id"]
    src = d["parse_result"]["source"]
    pending = [k for k, v in src.items() if v == "pending"]
    r = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=hdr(a["jwt"]))
    print(f"\n待确认槽位={pending} confirm -> {r.status_code} {r.text[:220]}")
