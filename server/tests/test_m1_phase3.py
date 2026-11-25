"""Phase 3 (M1) 语音/AI 升级、方言匹配、自学习、懒建档单位升级与新手期回归集。"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import PersonalDict
from app.models.doc import DocLine
from app.models.inventory import Inventory, InventoryMovement, StockBatch
from app.models.org import Member
from app.services.cost_monitor import CostMonitor
from app.services.degrade import DegradeController
from app.services.voice_runtime import reset_shared_voice_state
from app.voice.alias_resolver import AliasResolver, ProductEntry
from app.voice.asr_calibration import calibrate_text
from app.voice.conflict_detector import ConflictContext, detect
from app.voice.llm_fallback import LlmFallback
from app.voice.llm_provider import (
    CountingLlmProvider,
    FailoverLlmProvider,
    FakeLlmProvider,
    LlmTimeoutError,
    RealLlmProvider,
    build_llm_provider,
)
from app.voice.prompt_builder import PromptContext
from app.voice.prompt_cache import PromptCache
from app.voice.unit_converter import InMemoryRatioProvider, convert_with_ratios


def _settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-m1-phase3-suite-32bytes",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    reset_shared_voice_state()
    application = create_app(_settings())
    database = application.state.database
    database.create_all()

    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-m1-boss"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        tenant_id = uuid.UUID(data["tenant_id"])
        store_id = uuid.UUID(data["store_id"])
        member_id = uuid.UUID(data["member_id"])

        product_id = uuid.uuid4()
        with database.session(bypass_scope=True) as session:
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    name="可乐",
                    spec="500ml",
                    base_unit="瓶",
                    last_purchase_price_fen=150,
                    last_sale_price_fen=250,
                )
            )
            session.add(
                UnitConversion(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    from_unit="箱",
                    to_unit="瓶",
                    ratio=Decimal("24"),
                    status="approved",
                )
            )

        yield {
            "client": client,
            "database": database,
            "headers": headers,
            "tenant_id": tenant_id,
            "store_id": store_id,
            "member_id": member_id,
            "product_id": product_id,
        }

    reset_shared_voice_state()
    database.drop_all()
    database.dispose()


def _say(
    client: TestClient,
    headers: dict[str, str],
    text: str,
    *,
    active_card_id: str | None = None,
    asr_confidence: float = 0.95,
) -> dict:
    sess = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2000, "energy_rms": 0.5},
        headers=headers,
    )
    assert sess.status_code == 200, sess.text
    session_id = sess.json()["data"]["session_id"]
    body: dict[str, object] = {"asr_text": text, "asr_confidence": asr_confidence}
    if active_card_id is not None:
        body["active_card_id"] = active_card_id
    parsed = client.post(
        f"/api/v1/voice/sessions/{session_id}/parse",
        json=body,
        headers=headers,
    )
    assert parsed.status_code == 200, parsed.text
    return parsed.json()["data"]


# --------------------------------------------------------------------------- M1-1: 多模型主备、防幻觉、缓存与成本监控
def test_failover_and_bailian_thinking_disabled() -> None:
    class _FailingPrimary:
        def parse(self, prompt: str, schema: dict) -> dict:
            raise LlmTimeoutError("primary timeout")

    backup = FakeLlmProvider(default={"intent": "stock.query", "slots": {"P": "可乐"}, "source": {"P": "asr"}, "confidence": 0.9})
    strong = FakeLlmProvider(default={"intent": "sales.create", "slots": {"P": "可乐", "Q": 1}, "source": {"P": "asr", "Q": "asr"}, "confidence": 0.92})
    wrapper = FailoverLlmProvider(_FailingPrimary(), backup=backup, strong=strong)

    out = wrapper.parse("查可乐库存", {})
    assert out["intent"] == "stock.query"
    assert wrapper.last_provider_role == "backup"

    strong_out = wrapper.parse_strong("难句", {})
    assert strong_out["intent"] == "sales.create"
    assert wrapper.last_provider_role == "strong"

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"intent": "stock.query"}'}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 12},
    }
    mock_client.post.return_value = mock_resp

    real = RealLlmProvider(
        api_key="k",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3-8b",
        vendor="bailian",
        client=mock_client,
    )
    res = real.parse("查库存", {"type": "object"})
    assert res["intent"] == "stock.query"
    assert mock_client.post.call_args.kwargs["json"]["enable_thinking"] is False


def test_build_llm_provider_assembles_failover_from_settings() -> None:
    s = Settings(
        env="test",
        llm_provider="real",
        llm_vendor="bailian",
        llm_api_key="k1",
        llm_base_url="https://a.example/v1",
        llm_model="qwen3-8b",
        llm_backup_api_key="k2",
        llm_backup_base_url="https://b.example/v1",
        llm_backup_model="deepseek-chat",
        llm_strong_model="qwen3-32b",
    )
    provider = build_llm_provider(s)
    assert isinstance(provider, CountingLlmProvider)
    assert isinstance(provider.inner, FailoverLlmProvider)


def test_verify_grounding_and_conflict_guard_and_cost_monitor() -> None:
    fake = FakeLlmProvider(
        fixtures={
            "盘点": {
                "intent": "purchase.create",
                "slots": {"P": "火星不存在饮料", "Q": 8, "U": "箱"},
                "source": {"P": "asr", "Q": "asr", "U": "asr"},
                "confidence": 0.92,
            }
        }
    )
    counter = CountingLlmProvider(fake)
    cache = PromptCache(max_entries=16, ttl_seconds=60)
    fallback = LlmFallback(counter, cache=cache, max_retries=0)
    ctx = PromptContext(
        user_text="盘点，仓库还剩八箱",
        today_iso="2026-11-25",
        tenant_id="t1",
        member_id="m1",
        dict_version=1,
    )
    outcome = fallback.parse(ctx)
    assert outcome.ok is True
    assert outcome.result is not None
    assert outcome.result.route == "clarify", "LLM 把「还剩」错判为 purchase.create 必须被冲突检测拦成 clarify"
    assert outcome.result.source.get("P") == "pending", "凭空编造的商品名必须标 pending"
    assert outcome.usage["tokens_in"] > 0

    monitor = CostMonitor(prompt_cache=cache)
    monitor.record(
        stage="G4",
        llm_calls=1,
        tokens_in=outcome.usage["tokens_in"],
        tokens_out=outcome.usage["tokens_out"],
    )
    assert monitor.snapshot().tokens_in == outcome.usage["tokens_in"]

    # 缓存 key 必须按业务日 / member / dict_version 隔离
    ctx2 = PromptContext(
        user_text="盘点，仓库还剩八箱",
        today_iso="2026-11-26",
        tenant_id="t1",
        member_id="m1",
        dict_version=1,
    )
    with patch.object(counter, "parse", wraps=counter.parse) as spy:
        fallback.parse(ctx2)
        assert spy.call_count == 1


def test_degrade_try_auto_recover() -> None:
    ctrl = DegradeController()
    ctrl.decide(error_rate=0.5)
    assert ctrl.level == "L2"
    # 故障仍在时不恢复
    assert ctrl.try_auto_recover(error_rate=0.3) is None
    # 恢复后回到 L0
    dec = ctrl.try_auto_recover(error_rate=0.0)
    assert dec is not None and ctrl.level == "L0"
    # L3 额度用尽不自动恢复
    ctrl.decide(usage_ratio=1.0)
    assert ctrl.level == "L3"
    assert ctrl.try_auto_recover(error_rate=0.0, usage_ratio=0.0) is None


# --------------------------------------------------------------------------- M1-2: 同音误改防护、全量词表与方言模糊、2次自动学习
def test_asr_calibration_and_conflict_compound_guards() -> None:
    assert calibrate_text("进了十包五香花生").text == "进了十包五香花生"
    assert calibrate_text("进了五盒十三香").text == "进了五盒十三香"
    assert calibrate_text("进了两香可乐").text == "进了两箱可乐"
    assert calibrate_text("呃那个帮我记一下卖了三瓶雪碧").text == "卖了三瓶雪碧"

    assert detect(ConflictContext(text="卖了两盒退热贴", intent_id="sales.create")) == ()
    assert detect(ConflictContext(text="进了十个U盘", intent_id="purchase.create")) == ()
    assert detect(ConflictContext(text="卖了五盒减肥茶", intent_id="sales.create")) == ()


def test_personal_alias_uuid_and_phonetic_fuzzy_match() -> None:
    p_coca = ProductEntry(product_id="uuid-coca", name="可乐", spec="500ml", base_unit="瓶")
    p_sprite = ProductEntry(product_id="uuid-sprite", name="雪碧", spec="500ml", base_unit="瓶")
    resolver = AliasResolver([p_coca, p_sprite], personal_aliases={"快乐水": "uuid-coca"})

    hit = resolver.resolve("快乐水")
    assert hit is not None and hit.product == p_coca and hit.is_alias is True

    fuzzy = resolver.fuzzy_match("可了")
    assert fuzzy is not None and fuzzy.product == p_coca

    similars = resolver.find_similar("学毕")
    assert p_sprite in similars


def test_catalog_over_20_products_and_twice_correction_auto_promotes(shop: dict[str, object]) -> None:
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    database = shop["database"]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]

    # 插入第 21..25 个商品，确保不会被 Top20 prompt 预算截断规则层匹配
    with database.session(bypass_scope=True) as session:
        for i in range(25):
            session.add(
                Product(
                    tenant_id=tenant_id,
                    name=f"测试零食{i}",
                    base_unit="包",
                    last_purchase_price_fen=500,
                    last_sale_price_fen=800,
                )
            )
    d25 = _say(client, headers, "进了两包测试零食0")
    assert d25["route"] == "card"
    assert d25["card"]["calc"]["lines"][0]["pending"] is False

    # 连续两次把「口乐」改回「可乐 500ml」 -> 第 2 次触发自动写入词表并提示「记住了」
    d1 = _say(client, headers, "进了两箱口乐，一件三十五")
    cid1 = d1["card"]["meta"]["card_id"]
    e1 = client.patch(f"/api/v1/voice/cards/{cid1}/edit", json={"slots": [{"code": "P", "value": "可乐 500ml"}]}, headers=headers)
    assert e1.status_code == 200, e1.text
    assert "toast_zh" not in e1.json()["data"]

    d2 = _say(client, headers, "进了两箱口乐，一件三十五")
    cid2 = d2["card"]["meta"]["card_id"]
    e2 = client.patch(f"/api/v1/voice/cards/{cid2}/edit", json={"slots": [{"code": "P", "value": "可乐 500ml"}]}, headers=headers)
    assert e2.status_code == 200, e2.text
    data2 = e2.json()["data"]
    assert data2.get("toast_zh") == "记住了：口乐 → 可乐 500ml"
    assert data2["calc"]["lines"][0]["product_id"] == str(shop["product_id"])
    assert data2["calc"]["lines"][0]["pending"] is False

    with database.session(bypass_scope=True) as session:
        aliases = list(session.execute(select(ProductAlias).where(ProductAlias.tenant_id == tenant_id)).scalars())
        assert any(a.alias_text == "口乐" for a in aliases)
        pdicts = list(session.execute(select(PersonalDict).where(PersonalDict.tenant_id == tenant_id)).scalars())
        assert any(d.raw_text == "口乐" for d in pdicts)


# --------------------------------------------------------------------------- M1-3: 度量衡自动换算 & 懒建档原子基本单位升级
def test_standard_weight_conversion_and_atomic_base_unit_upgrade(shop: dict[str, object]) -> None:
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    database = shop["database"]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]

    out = convert_with_ratios(3, "公斤", "斤", product_id="p1", provider=InMemoryRatioProvider())
    assert out.ok is True and out.qty_base == Decimal("6.0000") and out.needs_teaching is False

    pkg = convert_with_ratios(3, "箱", "瓶", product_id="p1", provider=InMemoryRatioProvider())
    assert pkg.ok is False and pkg.needs_teaching is True

    # 先以「箱」懒建档进货 10 箱芬达（每箱 24 元 = 2400 分）
    first = _say(client, headers, "进了10箱芬达，一箱24元")
    assert first["route"] == "card"
    cid = first["card"]["meta"]["card_id"]
    c_resp = client.post(
        f"/api/v1/voice/cards/{cid}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert c_resp.status_code == 200, c_resp.text

    with database.session(bypass_scope=True) as session:
        fanta = session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == "芬达")
        ).scalar_one()
        fanta_id = fanta.product_id
        assert fanta.base_unit == "箱"

    # 教学 1 箱 = 24 瓶 -> 原子把芬达基本单位升级成「瓶」，旧库存 10 箱变 240 瓶，单箱成本 2400 分变 100 分
    up = client.post(
        f"/api/v1/products/{fanta_id}/unit-ratio",
        json={"from_unit": "箱", "to_unit": "瓶", "ratio": 24},
        headers=headers,
    )
    assert up.status_code == 200, up.text
    ur = up.json()["data"]["unit_ratio"]
    assert ur["upgraded_base_unit"] is True
    assert ur["base_unit"] == "瓶"

    with database.session(bypass_scope=True) as session:
        fanta_after = session.get(Product, fanta_id)
        assert fanta_after is not None and fanta_after.base_unit == "瓶"
        assert fanta_after.last_purchase_price_fen == 100
        inv = session.execute(
            select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == fanta_id)
        ).scalar_one()
        assert Decimal(inv.qty_base) == Decimal("240")
        assert inv.avg_cost_fen == 100
        move = session.execute(
            select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id, InventoryMovement.product_id == fanta_id)
        ).scalar_one()
        assert Decimal(move.qty_base) == Decimal("240")
        dline = session.execute(
            select(DocLine).where(DocLine.tenant_id == tenant_id, DocLine.product_id == fanta_id)
        ).scalar_one()
        assert Decimal(dline.qty_base) == Decimal("240")
        assert str(dline.unit) == "箱"


# --------------------------------------------------------------------------- M1-4: 新手期 20 笔、毕业直记、/me、字段重说、片段追述
def test_novice_gate_graduation_auto_post_me_field_voice_and_fragment(shop: dict[str, object]) -> None:
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    database = shop["database"]
    member_id: uuid.UUID = shop["member_id"]  # type: ignore[assignment]

    me0 = client.get("/api/v1/me", headers=headers)
    assert me0.status_code == 200, me0.text
    assert me0.json()["data"]["novice"]["in_novice"] is True
    assert me0.json()["data"]["novice"]["target"] == 20

    # 字段级语音重说（0 LLM）与片段追述（0 LLM）
    parsed = _say(client, headers, "进了两箱可乐，一件三十五")
    card_id = parsed["card"]["meta"]["card_id"]

    fv = client.post(
        f"/api/v1/voice/cards/{card_id}/field-voice",
        json={"slot_code": "Q", "spoken_text": "五箱"},
        headers=headers,
    )
    assert fv.status_code == 200, fv.text
    fv_data = fv.json()["data"]
    assert fv_data["llm_calls"] == 0
    assert fv_data["calc"]["lines"][0]["quantity"] == 5

    frag = _say(client, headers, "一箱40", active_card_id=card_id)
    assert frag["route"] == "card_updated"
    assert frag["llm_calls"] == 0
    assert frag["card"]["calc"]["lines"][0]["amount_fen"] == 20000

    # 把新手计数推进到 19 笔且准确率 ≥70%，第 20 笔确认后应触发毕业并开启 A 档直记
    with database.session(bypass_scope=True) as session:
        m = session.get(Member, member_id)
        assert m is not None
        m.novice_voice_count = 19
        m.novice_first_pass_count = 18

    # 用一条未经 edit 过的卡片确认作为第 20 笔
    p20 = _say(client, headers, "进了两箱可乐，一件三十五")
    assert p20["route"] == "card"
    c20 = client.post(
        f"/api/v1/voice/cards/{p20['card']['meta']['card_id']}/confirm",
        json={"confirm_type": "button", "button_fallback": True},
        headers=headers,
    )
    assert c20.status_code == 200, c20.text
    novice20 = c20.json()["data"]["result"]["novice"]
    assert novice20["count"] == 20
    assert novice20["graduated"] is True
    assert novice20["just_graduated"] is True

    # 毕业后简单老货现结单走 Tier A 直记
    auto = _say(client, headers, "进了两箱可乐，一件三十五")
    assert auto["route"] == "auto_posted", auto
    assert auto["auto_posted"] is True
    assert auto["result"]["confirmed"] is True
    assert auto["undo_window_seconds"] == 5

    # 但赊账 / 陌生新货仍必须走确认流程（B/C 档保护，绝不直记）
    credit = _say(client, headers, "卖给老王一箱可乐，一箱40，先欠着")
    assert credit["route"] in ("card", "clarify"), credit
    assert not credit.get("auto_posted")
    new_prod = _say(client, headers, "进了两箱全新气泡水，一箱三十五")
    assert new_prod["route"] == "card", new_prod
    assert new_prod["auto_posted"] is False

    # 用户可随时在「我的」切回「每笔都确认」
    pref = client.patch(
        "/api/v1/me/preferences",
        json={"auto_post_enabled": False, "accounting_mode": "count_derive"},
        headers=headers,
    )
    assert pref.status_code == 200, pref.text
    assert pref.json()["data"]["auto_post_enabled"] is False
    assert pref.json()["data"]["accounting_mode"] == "count_derive"

    still_card = _say(client, headers, "进了两箱可乐，一件三十五")
    assert still_card["route"] == "card"
