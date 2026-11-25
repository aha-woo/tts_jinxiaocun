"""Phase 4（M2）回归与新能力验收测试：

1. 中国开源多模态视觉模型（Qwen2.5-VL / GLM-4V）配置校验与 Bailian 请求体装配；
2. 拍进货单全链路：解析（合计数不一致预警 + 新货标记 + Attachment 草稿 + Audit）→ 核对入库（自动建新商品、写库存与批次、赊账应付、DocHeader.source='photo'、Attachment.doc_id 回填、防重复确认、跨租户隔离）；
3. 拍进货单遇到已建档商品单位不一致时要求/写入换算关系；
4. 经营报表按 `today` / `week` / `month` 实时统计毛利与营业额，且店员严格隐藏进价/毛利；
5. 真实 UTF-8 BOM CSV 导出（`POST /exports` 即时完成 + `GET /exports/{id}/content` 租户隔离 + 公式注入中性化）。
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product
from app.models.doc import ArAp, DocHeader
from app.models.inventory import Inventory
from app.models.ops import Attachment, AuditLog
from app.models.org import Member, Role
from app.services.voice_runtime import reset_shared_voice_state
from app.services.wx_provider import FakeWxProvider
from app.voice.vision_provider import FakeVisionProvider, RealVisionProvider, build_vision_provider


@pytest.fixture()
def m2_env():
    reset_shared_voice_state()
    settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-for-m2-phase4-suite-32bytes",
        database_url="sqlite+pysqlite:///:memory:",
        llm_provider="fake",
        vision_provider="fake",
    )
    app = create_app(settings=settings)
    database = app.state.database
    database.create_all()

    with TestClient(app) as client:
        def _login(code: str) -> dict:
            resp = client.post("/api/v1/auth/login", json={"code": code})
            assert resp.status_code == 200, resp.text
            return resp.json()["data"]

        boss_data = _login("wx-m2-boss-a")
        other_data = _login("wx-m2-boss-b")
        tenant_a = uuid.UUID(boss_data["tenant_id"])
        store_a = uuid.UUID(boss_data["store_id"])

        clerk_openid = FakeWxProvider().resolve_openid("wx-m2-clerk-a").openid
        with database.session(bypass_scope=True) as s:
            clerk_member = Member(
                tenant_id=tenant_a,
                wx_openid=clerk_openid,
                name="店员小张",
                status="active",
            )
            s.add(clerk_member)
            s.flush()
            s.add(
                Role(
                    tenant_id=tenant_a,
                    member_id=clerk_member.member_id,
                    store_id=store_a,
                    role_code="clerk",
                )
            )
            s.add(
                Product(
                    tenant_id=tenant_a,
                    name="可乐",
                    spec="500ml",
                    base_unit="箱",
                    last_purchase_price_fen=3500,
                    last_sale_price_fen=4500,
                    avg_cost_fen=3500,
                    status="active",
                )
            )
            s.add(
                Product(
                    tenant_id=tenant_a,
                    name="鲜牛奶",
                    spec="250ml",
                    base_unit="盒",
                    last_purchase_price_fen=300,
                    avg_cost_fen=300,
                    status="active",
                )
            )

        clerk_data = _login("wx-m2-clerk-a")
        assert clerk_data["tenant_id"] == str(tenant_a)
        assert clerk_data["role"] == "clerk"

        yield {
            "client": client,
            "database": database,
            "tenant_a": tenant_a,
            "store_a": store_a,
            "boss_headers": {"Authorization": f"Bearer {boss_data['jwt']}"},
            "clerk_headers": {"Authorization": f"Bearer {clerk_data['jwt']}"},
            "other_headers": {"Authorization": f"Bearer {other_data['jwt']}"},
        }


def test_vision_provider_presets_and_real_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError):
        FakeVisionProvider(is_production=True)

    with pytest.raises(ValueError):
        build_vision_provider(
            Settings(env="test", vision_provider="real", vision_api_key="", vision_base_url="", vision_model="")
        )

    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "supplier_name": "百炼供货",
                                    "payment_status": "已结",
                                    "stated_total_yuan": 70.0,
                                    "lines": [
                                        {
                                            "product_name": "可乐",
                                            "unit": "箱",
                                            "quantity": 2,
                                            "unit_price_yuan": 35.0,
                                            "amount_yuan": 70.0,
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }

    def _fake_post(url: str, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = RealVisionProvider(
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen2.5-vl-72b-instruct",
        vendor="bailian",
        timeout_seconds=15.0,
    )
    out = provider.extract(image_base64="YWJjZA==")
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["json"]["enable_thinking"] is False  # type: ignore[index]
    assert out.stated_total_fen == 7000
    assert len(out.lines) == 1
    assert out.lines[0].unit_price_fen == 3500


def test_delivery_note_parse_mismatch_new_product_and_confirm_photo_doc(m2_env: dict) -> None:
    client: TestClient = m2_env["client"]
    database = m2_env["database"]
    tenant_a: uuid.UUID = m2_env["tenant_a"]
    boss = m2_env["boss_headers"]
    other = m2_env["other_headers"]

    # 使用 hint_text 模拟多模态识别 JSON（含老货可乐 + 新货无糖雪碧 + 故意写错手写合计数）
    fake_ocr = {
        "supplier_name": "兴旺批发部",
        "payment_status": "未结",
        "stated_total_fen": 13000,
        "lines": [
            {"product_name": "可乐", "unit": "箱", "quantity": 2, "unit_price_fen": 3500, "amount_fen": 7000},
            {"product_name": "无糖雪碧", "unit": "箱", "quantity": 1, "unit_price_fen": 4200, "amount_fen": 4200},
        ],
    }
    res_parse = client.post(
        "/api/v1/delivery-note/parse",
        json={"object_ref": "cloud://dn/001.jpg", "hint_text": json.dumps(fake_ocr, ensure_ascii=False)},
        headers=boss,
    )
    assert res_parse.status_code == 200, res_parse.text
    parsed = res_parse.json()["data"]
    att_id = parsed["attachment_id"]
    assert parsed["total_mismatch"] is True
    assert parsed["computed_total_fen"] == 11200
    assert parsed["total_diff_fen"] == 1800
    assert parsed["lines"][0]["is_new_product"] is False
    assert parsed["lines"][1]["is_new_product"] is True
    assert any("新货" in w for w in parsed["warnings"])

    # 跨租户不可确认 A 商户进货单
    res_cross = client.post(
        f"/api/v1/delivery-note/{att_id}/confirm",
        json={
            "supplier_name": "兴旺批发部",
            "payment_status": "已结",
            "lines": [{"product_name": "可乐", "unit": "箱", "quantity": 2, "unit_price_fen": 3500}],
        },
        headers=other,
    )
    assert res_cross.status_code == 404

    # 商户核对后正式入库（赊账 + 新货自动建档）
    res_confirm = client.post(
        f"/api/v1/delivery-note/{att_id}/confirm",
        json={
            "supplier_name": "兴旺批发部",
            "payment_status": "未结",
            "stated_total_fen": 11200,
            "lines": [
                {
                    "product_name": "可乐",
                    "unit": "箱",
                    "quantity": 2,
                    "unit_price_fen": 3500,
                    "amount_fen": 7000,
                    "matched_product_id": parsed["lines"][0]["matched_product_id"],
                },
                {
                    "product_name": "无糖雪碧",
                    "unit": "箱",
                    "quantity": 1,
                    "unit_price_fen": 4200,
                    "amount_fen": 4200,
                },
            ],
        },
        headers=boss,
    )
    assert res_confirm.status_code == 200, res_confirm.text
    confirmed = res_confirm.json()["data"]
    assert confirmed["source"] == "photo"
    assert confirmed["total_amount_fen"] == 11200
    assert confirmed["line_count"] == 2
    doc_id = uuid.UUID(confirmed["doc_id"])

    # 不可重复确认同一张进货单
    res_dup = client.post(
        f"/api/v1/delivery-note/{att_id}/confirm",
        json={
            "supplier_name": "兴旺批发部",
            "payment_status": "未结",
            "lines": [{"product_name": "可乐", "unit": "箱", "quantity": 2, "unit_price_fen": 3500}],
        },
        headers=boss,
    )
    assert res_dup.status_code == 409

    # 查库验收：DocHeader.source=='photo'、Attachment.doc_id、新货雪碧已建档并入库、应付账款已挂账、审计已落库
    with database.session(bypass_scope=True) as s:
        doc = s.execute(select(DocHeader).where(DocHeader.doc_id == doc_id)).scalar_one()
        assert doc.source == "photo"
        att = s.execute(select(Attachment).where(Attachment.attachment_id == uuid.UUID(att_id))).scalar_one()
        assert att.doc_id == doc_id
        sprite = s.execute(select(Product).where(Product.tenant_id == tenant_a, Product.name == "无糖雪碧")).scalar_one()
        assert sprite.base_unit == "箱"
        assert sprite.last_purchase_price_fen == 4200
        inv = s.execute(select(Inventory).where(Inventory.tenant_id == tenant_a, Inventory.product_id == sprite.product_id)).scalar_one()
        assert Decimal(inv.qty_base) == Decimal("1")
        payable = s.execute(select(ArAp).where(ArAp.tenant_id == tenant_a, ArAp.doc_id == doc_id)).scalar_one()
        assert payable.direction == "payable" and payable.amount_fen == 11200
        audit_actions = {
            row.action
            for row in s.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_a)).scalars()
        }
        assert {"delivery_note.parse", "delivery_note.confirm"}.issubset(audit_actions)

    listed = client.get("/api/v1/delivery-note/attachments", headers=boss)
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["doc_id"] == str(doc_id)


def test_delivery_note_unit_ratio_flow(m2_env: dict) -> None:
    client: TestClient = m2_env["client"]
    boss = m2_env["boss_headers"]
    ocr = {
        "supplier_name": "乳品配送",
        "payment_status": "已结",
        "stated_total_fen": 7200,
        "lines": [
            {"product_name": "鲜牛奶", "unit": "箱", "quantity": 2, "unit_price_fen": 3600, "amount_fen": 7200}
        ],
    }
    res = client.post(
        "/api/v1/delivery-note/parse",
        json={"object_ref": "cloud://dn/milk.jpg", "hint_text": json.dumps(ocr, ensure_ascii=False)},
        headers=boss,
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["lines"][0]["needs_unit_ratio"] is True

    missing_ratio = client.post(
        f"/api/v1/delivery-note/{data['attachment_id']}/confirm",
        json={"payment_status": "已结", "lines": data["lines"]},
        headers=boss,
    )
    assert missing_ratio.status_code == 422

    lines_with_ratio = [{**data["lines"][0], "unit_ratio": "12"}]
    ok_res = client.post(
        f"/api/v1/delivery-note/{data['attachment_id']}/confirm",
        json={"payment_status": "已结", "lines": lines_with_ratio},
        headers=boss,
    )
    assert ok_res.status_code == 200, ok_res.text


def test_query_report_week_and_month_with_cost_masking(m2_env: dict) -> None:
    client: TestClient = m2_env["client"]
    boss = m2_env["boss_headers"]
    clerk = m2_env["clerk_headers"]

    # 先通过语音主链记一笔进货 + 一笔销售
    for text in ("进了两箱可乐，一箱35", "卖了一箱可乐，一箱45"):
        sess = client.post(
            "/api/v1/voice/sessions",
            json={"duration_ms": 1200, "energy_rms": 0.5, "mode": "hold"},
            headers=boss,
        )
        sid = sess.json()["data"]["session_id"]
        parsed = client.post(
            f"/api/v1/voice/sessions/{sid}/parse",
            json={"asr_text": text, "asr_confidence": 0.96, "duration_ms": 1200},
            headers=boss,
        ).json()["data"]
        cid = parsed["card"]["meta"]["card_id"]
        conf = client.post(
            f"/api/v1/voice/cards/{cid}/confirm",
            json={"confirm_type": "button", "button_fallback": True},
            headers=boss,
        )
        assert conf.status_code == 200, conf.text

    for p in ("today", "week", "month"):
        r_boss = client.get(f"/api/v1/query/report?period={p}", headers=boss)
        assert r_boss.status_code == 200, r_boss.text
        rep = r_boss.json()["data"]["report"]
        assert rep["period"] == p
        assert rep["sales_fen"] == 4500
        assert rep["purchase_fen"] == 7000
        assert rep["gross_profit_fen"] == 1000
        assert rep["cost_hidden"] is False

        r_clerk = client.get(f"/api/v1/query/report?period={p}", headers=clerk)
        assert r_clerk.status_code == 200
        crep = r_clerk.json()["data"]["report"]
        assert crep["cost_hidden"] is True
        assert crep["purchase_fen"] is None
        assert crep["gross_profit_fen"] is None
        assert crep["sales_fen"] == 4500


def test_real_export_csv_bom_formula_safe_and_tenant_isolated(m2_env: dict) -> None:
    client: TestClient = m2_env["client"]
    database = m2_env["database"]
    tenant_a: uuid.UUID = m2_env["tenant_a"]
    store_a: uuid.UUID = m2_env["store_a"]
    boss = m2_env["boss_headers"]
    clerk = m2_env["clerk_headers"]
    other = m2_env["other_headers"]

    # 插入一条带公式注入前缀的商品库存，测试 CSV 安全转义
    with database.session(bypass_scope=True) as s:
        bad_prod = Product(
            tenant_id=tenant_a,
            name="=CMD|calc",
            spec="",
            base_unit="个",
            last_purchase_price_fen=500,
            avg_cost_fen=500,
            status="active",
        )
        s.add(bad_prod)
        s.flush()
        s.add(Inventory(tenant_id=tenant_a, store_id=store_a, product_id=bad_prod.product_id, qty_base=Decimal("5")))

    exp = client.post("/api/v1/exports", json={"scope": "stock", "file_format": "csv"}, headers=boss)
    assert exp.status_code == 200, exp.text
    job = exp.json()["data"]["export"]
    assert job["status"] == "done"
    assert job["row_count"] >= 1
    job_id = job["job_id"]

    # 店员不可读取导出内容，租户 B 不可越权读取
    assert client.get(f"/api/v1/exports/{job_id}/content", headers=clerk).status_code == 403
    assert client.get(f"/api/v1/exports/{job_id}/content", headers=other).status_code == 404

    content_res = client.get(f"/api/v1/exports/{job_id}/content", headers=boss)
    assert content_res.status_code == 200, content_res.text
    body = content_res.json()["data"]
    csv_text = body["csv_content"]
    assert csv_text.startswith("\ufeffproduct_name,")
    assert "'=CMD|calc" in csv_text
