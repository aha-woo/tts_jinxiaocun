"""V1 补齐能力全链路回归测试（涵盖一 1–4 与二 1–5 全部 9 项功能）。"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, ProductAlias
from app.models.doc import ArAp, DocHeader, DocLine
from app.models.inventory import Inventory, InventoryMovement, StockBatch
from app.models.org import Member
from app.models.pay import Entitlement, GoodsItem
from app.models.tenant import Tenant
from app.pay.wx_xpay_client import WxXpayClient, compute_pay_sig
from app.services.closing_summary import build_closing_summary, send_closing_summary
from app.services.reconcile import reconcile_store
from app.services.voice_runtime import reset_shared_voice_state
from app.voice.asr_provider import RealAsrProvider, build_asr_provider


@pytest.fixture()
def v1_env():
    reset_shared_voice_state()
    settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-for-v1-completion-32bytes",
        database_url="sqlite+pysqlite:///:memory:",
        llm_provider="fake",
        vision_provider="fake",
        asr_provider="fake",
    )
    app = create_app(settings=settings)
    database = app.state.database
    database.create_all()

    with TestClient(app) as client:
        def _login(code: str) -> dict:
            r = client.post("/api/v1/auth/login", json={"code": code})
            assert r.status_code == 200, r.text
            return r.json()["data"]

        boss = _login("wx-v1-boss")
        other = _login("wx-v1-helper")
        tenant_id = uuid.UUID(boss["tenant_id"])
        store_id = uuid.UUID(boss["store_id"])

        with database.session(bypass_scope=True) as s:
            p1 = Product(
                tenant_id=tenant_id,
                name="可乐",
                spec="500ml",
                base_unit="瓶",
                last_purchase_price_fen=200,
                last_sale_price_fen=350,
                avg_cost_fen=200,
                track_batch=True,
                cost_method="batch",
                status="active",
            )
            p2 = Product(
                tenant_id=tenant_id,
                name="可口可乐",
                spec="500ml",
                base_unit="瓶",
                last_purchase_price_fen=220,
                last_sale_price_fen=350,
                avg_cost_fen=220,
                track_batch=True,
                cost_method="batch",
                status="active",
            )
            s.add_all(
                [
                    p1,
                    p2,
                    GoodsItem(
                        goods_id="quota_pack_500",
                        goods_name="语音加油包（500 次）",
                        goods_type="quota_pack",
                        price_fen=1000,
                        quota_amount=500,
                        validity_days=0,
                        status="on_shelf",
                    ),
                ]
            )
            s.flush()
            p1_id = p1.product_id
            p2_id = p2.product_id

        yield {
            "app": app,
            "client": client,
            "database": database,
            "settings": settings,
            "tenant_id": tenant_id,
            "store_id": store_id,
            "boss": boss,
            "other": other,
            "boss_headers": {"Authorization": f"Bearer {boss['jwt']}"},
            "other_headers": {"Authorization": f"Bearer {other['jwt']}"},
            "p1_id": p1_id,
            "p2_id": p2_id,
        }


def test_xpay_real_query_order_fallback_delivers_once(monkeypatch: pytest.MonkeyPatch, v1_env: dict) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    database = v1_env["database"]
    tenant_id: uuid.UUID = v1_env["tenant_id"]

    # 1. 下单
    ord_res = client.post("/api/v1/entitlement/order", json={"goods_id": "quota_pack_500"}, headers=boss_headers)
    assert ord_res.status_code == 200, ord_res.text
    out_trade_no = ord_res.json()["data"]["order"]["out_trade_no"]

    # 2. 注入真实微信 API 凭证并拦截 HTTP 请求，验证 xpay/query_order 签名与补发货
    monkeypatch.setattr(v1_env["app"].state.settings, "wx_appid", "wx_test_appid", raising=False) if False else None
    new_settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-for-v1-completion-32bytes",
        database_url="sqlite+pysqlite:///:memory:",
        wx_appid="wx_test_appid",
        wx_app_secret="wx_test_secret",
        wx_pay_app_key="wx_pay_key_123",
    )
    v1_env["app"].state.settings = new_settings

    import httpx

    calls: list[str] = []

    class _FakeHttpResp:
        def __init__(self, data: dict) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._data

    def _fake_get(url: str, **kwargs):
        calls.append(url)
        return _FakeHttpResp({"access_token": "tok_test_123", "expires_in": 7200})

    def _fake_post(url: str, **kwargs):
        calls.append(url)
        body_text = kwargs["content"].decode("utf-8")
        expected_sig = compute_pay_sig("/xpay/query_order", body_text, "wx_pay_key_123")
        assert kwargs["params"]["pay_sig"] == expected_sig
        return _FakeHttpResp({"errcode": 0, "order": {"status": 2}})

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)

    # 先验证 WxXpayClient 未配 key 时安全降级
    unconf = WxXpayClient(appid="", app_secret="", pay_app_key="").query_order(
        openid="o1", out_trade_no=out_trade_no
    )
    assert unconf.configured is False and unconf.paid is None

    # 调 query-order 兜底接口完成真实查单并补发货
    q_res = client.post(
        "/api/v1/entitlement/query-order",
        json={"out_trade_no": out_trade_no},
        headers=boss_headers,
    )
    assert q_res.status_code == 200, q_res.text
    result = q_res.json()["data"]["result"]
    assert result["pay_status"] == "paid", result["detail"]
    assert result["delivered"] is True, result["detail"]
    assert result["grant_quota"] > 0
    assert any("/xpay/query_order" in u for u in calls)

    from app.models.quota import UsageQuota

    with database.session(bypass_scope=True) as s:
        uq = s.execute(select(UsageQuota).where(UsageQuota.tenant_id == tenant_id)).scalars().first()
        assert uq is not None and uq.free_quota >= result["grant_quota"]


def test_cloud_asr_provider_with_tenant_hotwords(monkeypatch: pytest.MonkeyPatch, v1_env: dict) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    v1_env["app"].state.settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-for-v1-completion-32bytes",
        database_url="sqlite+pysqlite:///:memory:",
        asr_provider="real",
        asr_vendor="aliyun_paraformer",
        asr_endpoint="https://asr.example.invalid/v1/recognize",
        asr_api_key="asr-test-key",
    )

    import httpx

    captured: dict[str, object] = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"text": "进了十瓶可乐，一瓶两块", "confidence": 0.94}

    def _fake_post(url: str, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", _fake_post)

    assert isinstance(build_asr_provider(v1_env["app"].state.settings), RealAsrProvider)
    sess = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 1800, "energy_rms": 0.5, "mode": "hold"},
        headers=boss_headers,
    )
    sid = sess.json()["data"]["session_id"]

    # 仅传 audio_object_ref，不传 asr_text → 自动带本店商品名作为热词调用云 ASR
    parsed = client.post(
        f"/api/v1/voice/sessions/{sid}/parse",
        json={"audio_object_ref": "cloud://audio/test.mp3", "duration_ms": 1800},
        headers=boss_headers,
    )
    assert parsed.status_code == 200, parsed.text
    assert parsed.json()["data"]["route"] == "card"
    assert "可乐" in captured["payload"]["hotwords"]  # type: ignore[index]
    assert captured["payload"]["vendor"] == "aliyun_paraformer"  # type: ignore[index]


def test_product_merge_moves_inventory_docs_batches_and_aliases(v1_env: dict) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    database = v1_env["database"]
    tenant_id: uuid.UUID = v1_env["tenant_id"]
    p1_id: uuid.UUID = v1_env["p1_id"]
    p2_id: uuid.UUID = v1_env["p2_id"]

    # 分别以两个重复商品名进货
    for text in ("进了五瓶可乐，一瓶两块", "进了十瓶可口可乐，一瓶三块"):
        s = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.5}, headers=boss_headers)
        sid = s.json()["data"]["session_id"]
        pr = client.post(
            f"/api/v1/voice/sessions/{sid}/parse",
            json={"asr_text": text, "asr_confidence": 0.96, "duration_ms": 1500},
            headers=boss_headers,
        )
        cid = pr.json()["data"]["card"]["meta"]["card_id"]
        cf = client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=boss_headers)
        assert cf.status_code == 200, cf.text

    # 将 p2（可口可乐）合并入 p1（可乐）
    mg = client.post(
        f"/api/v1/products/{p2_id}/merge",
        json={"target_product_id": str(p1_id)},
        headers=boss_headers,
    )
    assert mg.status_code == 200, mg.text
    merge_data = mg.json()["data"]["merge"]
    assert merge_data["target_product_id"] == str(p1_id)
    assert merge_data["moved_aliases"] >= 1

    with database.session(bypass_scope=True) as s:
        src = s.get(Product, p2_id)
        assert src is not None and src.status == "merged" and src.merged_into == p1_id
        inv = s.execute(select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == p1_id)).scalar_one()
        assert Decimal(inv.qty_base) == Decimal("15")
        batches = list(s.execute(select(StockBatch).where(StockBatch.tenant_id == tenant_id, StockBatch.product_id == p1_id)).scalars())
        assert len(batches) == 2
        alias = s.execute(select(ProductAlias).where(ProductAlias.tenant_id == tenant_id, ProductAlias.alias_text == "可口可乐")).scalar_one()
        assert alias.product_id == p1_id


def test_debt_settle_and_partner_statement(v1_env: dict) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    database = v1_env["database"]
    tenant_id: uuid.UUID = v1_env["tenant_id"]

    # 先入库 10 瓶可乐再赊销给老王
    from app.models.partner import Partner

    with database.session(bypass_scope=True) as s:
        s.add(Partner(tenant_id=tenant_id, type="customer", name="老王"))

    for text, is_credit in (("进了十瓶可乐，一瓶2元", False), ("卖了5瓶可乐，一瓶4元", True)):
        s = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.5}, headers=boss_headers)
        sid = s.json()["data"]["session_id"]
        pr = client.post(
            f"/api/v1/voice/sessions/{sid}/parse",
            json={"asr_text": text, "asr_confidence": 0.96, "duration_ms": 1500},
            headers=boss_headers,
        ).json()["data"]
        if pr.get("route") == "clarify" and pr.get("clarify"):
            val = "未结" if (is_credit and pr["clarify"]["slot"] == "PS") else pr["clarify"]["options"][0]
            pr = client.post(
                f"/api/v1/voice/sessions/{sid}/clarify",
                json={"slot": pr["clarify"]["slot"], "value": val},
                headers=boss_headers,
            ).json()["data"]
        assert "card" in pr, pr
        cid = pr["card"]["meta"]["card_id"]
        if is_credit:
            ed = client.patch(
                f"/api/v1/voice/cards/{cid}/edit",
                json={"slots": [{"code": "PS", "value": "未结"}, {"code": "C", "value": "老王"}]},
                headers=boss_headers,
            )
            assert ed.status_code == 200, ed.text
        cf = client.post(
            f"/api/v1/voice/cards/{cid}/confirm",
            json={"confirm_type": "button", "button_fallback": True},
            headers=boss_headers,
        )
        assert cf.status_code == 200, cf.text
        assert cf.json()["data"]["result"]["confirmed"] is True, cf.json()

    debts = client.get("/api/v1/query/debt", headers=boss_headers).json()["data"]["items"]
    docs = client.get("/api/v1/query/docs", headers=boss_headers).json()["data"]["items"]
    assert len(debts) == 1, docs
    partner_id = debts[0]["partner_id"]
    assert debts[0]["outstanding_fen"] == 2000

    # 生成对账单
    stmt = client.get(f"/api/v1/debts/{partner_id}/statement?direction=receivable", headers=boss_headers)
    assert stmt.status_code == 200, stmt.text
    assert stmt.json()["data"]["total_outstanding_fen"] == 2000

    # 直接部分收款核销 12 元（1200 分）
    settle = client.post(
        "/api/v1/debts/settle",
        json={"partner_id": partner_id, "direction": "receivable", "amount_fen": 1200, "method": "微信"},
        headers=boss_headers,
    )
    assert settle.status_code == 200, settle.text
    assert settle.json()["data"]["remaining_outstanding_fen"] == 800

    with database.session(bypass_scope=True) as s:
        ar = s.execute(select(ArAp).where(ArAp.tenant_id == tenant_id)).scalar_one()
        assert ar.settled_amount_fen == 1200 and ar.status == "partial"


def test_count_open_voice_batch_and_count_derive_sales(v1_env: dict) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    database = v1_env["database"]
    tenant_id: uuid.UUID = v1_env["tenant_id"]

    # 进 10 瓶可乐（¥2 进价，¥3.5 售价）
    s = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.5}, headers=boss_headers)
    sid = s.json()["data"]["session_id"]
    cid = client.post(
        f"/api/v1/voice/sessions/{sid}/parse",
        json={"asr_text": "进了十瓶可乐，一瓶两块", "asr_confidence": 0.96, "duration_ms": 1500},
        headers=boss_headers,
    ).json()["data"]["card"]["meta"]["card_id"]
    client.post(f"/api/v1/voice/cards/{cid}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=boss_headers)

    # 切换成「进货 + 盘点倒推」模式
    pref = client.patch("/api/v1/me/preferences", json={"accounting_mode": "count_derive"}, headers=boss_headers)
    assert pref.status_code == 200

    # 开盘点并验证 GET /counts/open 可找回未结束盘点单
    c_start = client.post("/api/v1/counts", headers=boss_headers).json()["data"]
    count_id = c_start["count_id"]
    c_open = client.get("/api/v1/counts/open", headers=boss_headers).json()["data"]
    assert c_open["count"]["count_id"] == count_id

    # 语音连续报数盘点：「可乐还剩 4 瓶」→ 少 6 瓶应倒推出售 6 瓶（¥21.00）
    vb = client.post(
        f"/api/v1/counts/{count_id}/voice-batch",
        json={"spoken_text": "可乐还剩 4 瓶"},
        headers=boss_headers,
    )
    assert vb.status_code == 200, vb.text
    assert len(vb.json()["data"]["matched"]) == 1

    ap = client.post(f"/api/v1/counts/{count_id}/apply", headers=boss_headers)
    assert ap.status_code == 200, ap.text
    result = ap.json()["data"]["result"]
    assert result["accounting_mode"] == "count_derive"
    assert result["derived_sales_doc_id"] is not None
    assert result["derived_sales_amount_fen"] == 2100

    with database.session(bypass_scope=True) as s:
        sdoc = s.get(DocHeader, uuid.UUID(result["derived_sales_doc_id"]))
        assert sdoc is not None and sdoc.doc_type == "sales" and sdoc.cogs_fen == 1200


def test_staff_invite_join_disable_repeat_purchase_reconcile_and_closing_summary(
    monkeypatch: pytest.MonkeyPatch, v1_env: dict
) -> None:
    client: TestClient = v1_env["client"]
    boss_headers = v1_env["boss_headers"]
    other_headers = v1_env["other_headers"]
    database = v1_env["database"]
    tenant_id: uuid.UUID = v1_env["tenant_id"]
    store_id: uuid.UUID = v1_env["store_id"]

    # 1. 老板生成邀请码，另一个微信用户加入成为 clerk
    inv_res = client.post("/api/v1/me/staff/invite", json={"role_code": "clerk"}, headers=boss_headers)
    assert inv_res.status_code == 200, inv_res.text
    invite_code = inv_res.json()["data"]["invite_code"]

    join_res = client.post(
        "/api/v1/me/staff/join",
        json={"invite_code": invite_code, "display_name": "小李"},
        headers=other_headers,
    )
    assert join_res.status_code == 200, join_res.text
    joined = join_res.json()["data"]
    assert joined["tenant_id"] == str(tenant_id)
    assert joined["role"] == "clerk"
    clerk_headers = {"Authorization": f"Bearer {joined['jwt']}"}
    clerk_member_id = joined["member_id"]

    # 2. 先记一笔进货，再语音说「跟上次一样」复购出卡
    s1 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1500, "energy_rms": 0.5}, headers=boss_headers)
    c1 = client.post(
        f"/api/v1/voice/sessions/{s1.json()['data']['session_id']}/parse",
        json={"asr_text": "进了五瓶可乐，一瓶2元", "asr_confidence": 0.96, "duration_ms": 1500},
        headers=boss_headers,
    ).json()["data"]["card"]["meta"]["card_id"]
    client.post(f"/api/v1/voice/cards/{c1}/confirm", json={"confirm_type": "button", "button_fallback": True}, headers=boss_headers)

    s2 = client.post("/api/v1/voice/sessions", json={"duration_ms": 1200, "energy_rms": 0.5}, headers=clerk_headers)
    rep = client.post(
        f"/api/v1/voice/sessions/{s2.json()['data']['session_id']}/parse",
        json={"asr_text": "跟上次一样", "asr_confidence": 0.96, "duration_ms": 1200},
        headers=clerk_headers,
    )
    assert rep.status_code == 200, rep.text
    rep_data = rep.json()["data"]
    assert rep_data["route"] == "card"
    assert rep_data["reason"] == "repeat_purchase"
    assert rep_data["llm_calls"] == 0
    assert rep_data["card"]["calc"]["lines"][0]["quantity"] == 5

    # 3. 老板停用店员小李 → 店员旧 token 立即失效
    dis = client.patch(
        f"/api/v1/me/staff/{clerk_member_id}",
        json={"status": "disabled"},
        headers=boss_headers,
    )
    assert dis.status_code == 200
    assert client.get("/api/v1/me", headers=clerk_headers).status_code in (401, 403)

    # 4. 三账核对 + 收摊总结 dry_run + 破坏一致性验证告警检出
    rec = client.get("/api/v1/me/reconcile", headers=boss_headers)
    assert rec.status_code == 200
    assert rec.json()["data"]["ok"] is True

    cs_dry = client.post("/api/v1/me/closing-summary/send", headers=boss_headers)
    assert cs_dry.status_code == 200
    assert cs_dry.json()["data"]["dry_run"] is True
    assert cs_dry.json()["data"]["sent"] is False

    with database.session(bypass_scope=True) as s:
        # 人为制造一条库存漂移，验证 reconcile_store 检出 movement_mismatches
        inv_row = s.execute(select(Inventory).where(Inventory.tenant_id == tenant_id)).scalars().first()
        assert inv_row is not None
        inv_row.qty_base = Decimal(inv_row.qty_base) + Decimal("3")
        s.flush()
        bad_recon = reconcile_store(s, tenant_id=tenant_id, store_id=store_id)
        assert bad_recon["ok"] is False
        assert len(bad_recon["movement_mismatches"]) == 1

        # 测试订阅消息有模板凭证时能正确调用发送
        summary = build_closing_summary(s, tenant_id=tenant_id, store_id=store_id)
        import httpx

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **kw: type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"access_token": "tok"}})(),
        )
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **kw: type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"errcode": 0}})(),
        )
        real_send = send_closing_summary(
            Settings(wx_appid="wx1", wx_app_secret="sec1", wx_subscribe_template_id="tmpl_1"),
            openid="o_boss",
            summary=summary,
        )
        assert real_send["sent"] is True and real_send["dry_run"] is False
