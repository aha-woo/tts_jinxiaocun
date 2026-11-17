"""``sys.correct`` / ``sys.teach`` / ``product.alias.add`` 的**终态**回归。

为什么单独一个文件
------------------
Round 3 的交付阻塞条件是：「``sys.correct`` / ``sys.teach`` 现在是卡建得出来、
确认必失败」。顺着一查，``product.alias.add`` 也有同一类毛病，而且是**最容易被
忽略的一种**——它只在「商品有规格」时失败：

- 规则层把 ``P`` 填成 ``display_name()``（``"可乐 500ml"``）；
- 实体写入器按 ``Product.name`` 查（``"可乐"``）；
- 于是「可乐别名叫快乐水」这张卡 100% 撞 422「没有找到商品 可乐 500ml」，
  而 529 条测试里的商品大多没有 ``spec``，所以**一直是绿的**。

本文件的规矩和 ``test_main_chain_http`` 一致：**说人话、走真 HTTP、断言库里的终态**。
``sys.teach`` / ``sys.correct`` 在契约里是 ``template_hit=false``（只走 LLM），本地
fake LLM 没有 fixture，所以这里的做法是：**只替换「LLM 说这句话是什么意图」这一步**，
其余（路由、建卡、落账、词表、库存）全部是真的。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import PersonalDict
from app.models.doc import DocHeader
from app.models.inventory import Inventory
from app.voice.output_schema import ParseResult
from app.voice.pipeline import PipelineResult, VoicePipeline
from app.voice.teach import plan_teach, split_teach

#: 商品名带规格——正是这一条能照出「展示名 vs 档案名」的错位
PRODUCT_NAME = "可乐"
PRODUCT_SPEC = "500ml"
PRODUCT_DISPLAY = f"{PRODUCT_NAME} {PRODUCT_SPEC}"


# --------------------------------------------------------------------- fixtures
def _settings():
    from app.config import Settings

    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-meta-intents-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """跑起来的 app + 已登录租户 + 「可乐 500ml（箱/瓶=24）」+ 意图注入钩子。"""
    from app.main import create_app

    application = create_app(_settings())
    database = application.state.database
    database.create_all()

    #: ``{原话: intent_id}``；命中时把这一步的解析结果换成指定意图
    forced: dict[str, str] = {}
    #: ``{原话: {槽位: 值}}``；模拟「LLM 顺带把槽位也填了」（真实链路如此）
    forced_slots: dict[str, dict[str, Any]] = {}
    original_run = VoicePipeline.run

    def run(self, text, **kwargs):  # noqa: ANN001, ANN003 - 与真实签名一致
        key = (text or "").strip()
        forced_intent = forced.get(key)
        if forced_intent is None:
            return original_run(self, text, **kwargs)
        slots = dict(forced_slots.get(key) or {})
        parse = ParseResult(
            intent=forced_intent,
            slots=slots,
            # 必填槽位有值就标 ``asr``（与真实链路口径一致）
            source={code: "asr" for code in slots},
            confidence=0.9,
            channel="llm",
        )
        return PipelineResult(
            stage="G4",
            reason="forced_intent_for_test",
            parse=parse,
            llm_calls=1,
            asr_text=text,
            asr_confidence=0.95,
            trace=("G4:forced",),
        )

    monkeypatch.setattr(VoicePipeline, "run", run)

    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-meta-intents-1"})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        tenant_id = uuid.UUID(data["tenant_id"])

        product_id = uuid.uuid4()
        with database.session(bypass_scope=True) as session:
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    name=PRODUCT_NAME,
                    spec=PRODUCT_SPEC,
                    base_unit="瓶",
                    last_purchase_price_fen=3500,
                    last_sale_price_fen=4000,
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

        #: 供用例补充商品/单据
        def add_product(name: str, spec: str = "") -> uuid.UUID:
            new_id = uuid.uuid4()
            with database.session(bypass_scope=True) as session:
                session.add(
                    Product(
                        tenant_id=tenant_id,
                        product_id=new_id,
                        name=name,
                        spec=spec,
                        base_unit="瓶",
                        last_purchase_price_fen=100,
                        last_sale_price_fen=150,
                    )
                )
            return new_id

        yield {
            "client": client,
            "database": database,
            "headers": headers,
            "tenant_id": tenant_id,
            "product_id": product_id,
            "forced": forced,
            "forced_slots": forced_slots,
            "add_product": add_product,
        }

    database.drop_all()
    database.dispose()


# --------------------------------------------------------------------- helpers
def _say(client: TestClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    session = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    assert session.status_code == 200, session.text
    session_id = session.json()["data"]["session_id"]
    parsed = client.post(
        f"/api/v1/voice/sessions/{session_id}/parse",
        json={"asr_text": text},
        headers=headers,
    )
    assert parsed.status_code == 200, parsed.text
    return parsed.json()["data"]


def _confirm(client: TestClient, headers: dict[str, str], card_id: str, **extra: Any):
    return client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", **extra},
        headers=headers,
    )


def _inventory(database, tenant_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    with database.session(bypass_scope=True) as session:
        row = session.execute(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id, Inventory.product_id == product_id
            )
        ).scalar_one_or_none()
    return Decimal("0") if row is None else Decimal(row.qty_base)


# ==================================================== ① 切分：纯文本层（不碰库）
def test_split_teach_direction_is_not_flipped() -> None:
    """「可乐又叫快乐水」与「快乐水就是可乐」**方向相反**，不许切反。"""
    assert split_teach("可乐别名叫「快乐水」").alias == "快乐水"
    assert split_teach("可乐别名叫「快乐水」").target == "可乐"
    assert split_teach("可乐又叫快乐水").alias == "快乐水"
    assert split_teach("可乐又叫快乐水").target == "可乐"
    assert split_teach("以后我说「快乐水」就是可乐").alias == "快乐水"
    assert split_teach("以后我说「快乐水」就是可乐").target == "可乐"
    assert split_teach("教我说法").target is None


def test_plan_teach_is_deterministic_and_never_guesses() -> None:
    """指向解析不出来时必须 ``unknown_target``，**不许**编一个商品出来。"""

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name
            self.product_id = str(uuid.uuid4())

    class _Hit:
        def __init__(self, entry: _Entry | None, candidates: tuple = (), ambiguous=False) -> None:
            self.product = entry
            self.candidates = candidates
            self.ambiguous = ambiguous
            self.is_alias = False

    class _Resolver:
        def __init__(self) -> None:
            self.lexicon = type("L", (), {"entries": lambda self: (_Entry("可乐"), _Entry("雪碧"))})()

        def resolve(self, text: str):
            return _Hit(_Entry("可乐")) if text == "可乐" else None

        def scan(self, text: str):
            return ()

        def options_for(self, hit, limit=3):  # noqa: ANN001
            return ("可乐 500ml", "可乐 2L")

    resolver = _Resolver()
    ok = plan_teach(text="以后我说快乐水就是可乐", resolver=resolver)
    assert ok.ok and ok.target is not None
    assert ok.target.product_name == "可乐"

    missing = plan_teach(text="以后我说小可就是芬达", resolver=resolver)
    assert missing.reason == "unknown_target"
    assert missing.target is None
    assert missing.candidates == ("可乐", "雪碧")

    no_alias = plan_teach(text="记住", resolver=resolver)
    assert no_alias.reason in {"no_alias", "no_target"}


# ============================================ ② 别名：带规格的商品也要能确认
def test_alias_add_on_product_with_spec_is_confirmable(shop: dict[str, Any]) -> None:
    """「可乐别名叫快乐水」→ 确认 200（不是 422「没有找到商品 可乐 500ml」）。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]

    data = _say(client, headers, "可乐别名叫'快乐水'")
    assert data["route"] == "card", data
    # 前置事实：P 确实是「展示名」（否则这条用例照不出错位）
    assert data["parse_result"]["slots"]["P"] == PRODUCT_DISPLAY

    response = _confirm(client, headers, data["card"]["meta"]["card_id"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"]["confirmed"] is True

    with database.session(bypass_scope=True) as session:
        alias = session.execute(
            select(ProductAlias).where(
                ProductAlias.tenant_id == tenant_id,
                ProductAlias.product_id == product_id,
                ProductAlias.alias_text == "快乐水",
            )
        ).scalar_one_or_none()
        personal = session.execute(
            select(PersonalDict).where(
                PersonalDict.tenant_id == tenant_id, PersonalDict.raw_text == "快乐水"
            )
        ).scalar_one_or_none()
    assert alias is not None, "说了「别名」却没写 product_alias —— 下一句照样认不出来"
    assert personal is not None, "sys.teach 契约说落个人词表，这里没落"


def test_taught_alias_takes_effect_on_the_next_sentence(shop: dict[str, Any]) -> None:
    """教完立刻生效：下一句「进了两箱快乐水」必须认成「可乐」，库存 48。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]

    teach = _say(client, headers, "可乐别名叫'快乐水'")
    assert _confirm(client, headers, teach["card"]["meta"]["card_id"]).status_code == 200

    purchase = _say(client, headers, "进了两箱快乐水，一件三十五")
    assert purchase["route"] == "card", purchase
    slots = purchase["parse_result"]["slots"]
    print(f"\n[教说法生效] 「快乐水」-> {slots.get('P')} source={purchase['parse_result']['source']}")
    assert slots.get("P") == PRODUCT_DISPLAY, f"教完还是没认出来：{slots}"
    assert purchase["parse_result"]["source"].get("P") == "alias"

    confirmed = _confirm(client, headers, purchase["card"]["meta"]["card_id"])
    assert confirmed.status_code == 200, confirmed.text
    assert _inventory(database, tenant_id, product_id) == Decimal("48")


# ==================================================== ③ sys.teach：不建坏卡
def test_teach_rewrites_to_alias_add_instead_of_building_a_broken_card(
    shop: dict[str, Any],
) -> None:
    """LLM 判成 ``sys.teach`` → 路由层改写成 ``product.alias.add`` → 确认真的写进库。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]
    shop["forced"]["以后我说'快乐水'就是可乐"] = "sys.teach"  # type: ignore[index]

    data = _say(client, headers, "以后我说'快乐水'就是可乐")
    assert data["route"] == "card", data
    # 契约没被改：对外仍然是「教说法」，只是落地走别名这条路
    assert data["teach"]["original_intent"] == "sys.teach"
    assert data["teach"]["written_as"] == "product.alias.add"
    assert data["parse_result"]["intent"] == "product.alias.add"
    assert data["parse_result"]["slots"] == {"P": PRODUCT_NAME, "ALIAS": "快乐水"}

    response = _confirm(client, headers, data["card"]["meta"]["card_id"])
    assert response.status_code == 200, response.text
    with database.session(bypass_scope=True) as session:
        alias = session.execute(
            select(ProductAlias).where(
                ProductAlias.tenant_id == tenant_id,
                ProductAlias.product_id == product_id,
                ProductAlias.alias_text == "快乐水",
            )
        ).scalar_one_or_none()
    assert alias is not None, "教完没写别名：这句话等于白说"


def test_teach_with_unknown_target_asks_instead_of_building_a_card(
    shop: dict[str, Any],
) -> None:
    """指向的商品不存在 → **不建卡**，走澄清给 2~3 个候选（不许瞎配）。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    shop["forced"]["以后我说小可就是芬达"] = "sys.teach"  # type: ignore[index]

    data = _say(client, headers, "以后我说小可就是芬达")
    print(f"\n[sys.teach 澄清] {data.get('clarify')} teach={data.get('teach')}")
    assert data["route"] == "clarify", data
    assert "card" not in data
    assert data["teach"]["reason"] == "unknown_target"
    options = data["clarify"]["options"]
    assert 2 <= len(options) <= 3, options
    assert data["clarify"]["slot"] == "P"


def test_teach_ambiguous_target_offers_candidates(shop: dict[str, Any]) -> None:
    """「大瓶」能对上好几个商品 → 澄清，不是随便挑一个写进去。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    add_product = shop["add_product"]
    #: 「可乐」有 500ml 与 2L 两种，说「大瓶」时没有唯一指向
    add_product("可乐", "2L")
    shop["forced"]["以后我说大瓶就是可乐"] = "sys.teach"  # type: ignore[index]

    data = _say(client, headers, "以后我说大瓶就是可乐")
    print(f"\n[sys.teach 歧义] {data.get('teach')} clarify={data.get('clarify')}")
    assert data["route"] in {"clarify", "card"}, data
    if data["route"] == "clarify":
        assert 2 <= len(data["clarify"]["options"]) <= 3


# ==================================================== ④ sys.correct：两步走完
def test_correct_card_is_confirmable_and_returns_next_step(shop: dict[str, Any]) -> None:
    """「更正上一单」→ 卡可确认 → 反做成功 + 回执带「请重说一句」。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]
    shop["forced"]["把上一单更正一下"] = "sys.correct"  # type: ignore[index]
    # 真实 LLM 会把 DOC/TV 一起填出来（契约里两个都是必填）
    shop["forced_slots"]["把上一单更正一下"] = {"DOC": "上一单", "TV": 8000}  # type: ignore[index]

    purchase = _say(client, headers, "进了两箱可乐，一件三十五")
    assert purchase["route"] == "card", purchase
    first = _confirm(client, headers, purchase["card"]["meta"]["card_id"])
    assert first.status_code == 200, first.text
    doc_no = first.json()["data"]["result"]["doc_no"]
    assert _inventory(database, tenant_id, product_id) == Decimal("48")

    corrected = _say(client, headers, "把上一单更正一下")
    assert corrected["route"] == "card", corrected
    assert corrected["next_step_zh"], "没提前说清楚「还有第二步」，用户会以为已经改好了"

    response = _confirm(client, headers, corrected["card"]["meta"]["card_id"],
                        confirm_type="voice2", spoken_text="确认")
    assert response.status_code == 200, response.text
    body = response.json()["data"]["result"]
    print(f"\n[sys.correct] {body.get('doc_no')} follow_up={body.get('follow_up')}")
    assert body["confirmed"] is True
    assert body["follow_up"]["action"] == "rerecord"
    assert body["follow_up"]["step"] == 2
    assert doc_no in body["follow_up"]["hint_zh"]
    # 反做真的发生了：库存回 0，原单 reversed
    assert _inventory(database, tenant_id, product_id) == Decimal("0")
    with database.session(bypass_scope=True) as session:
        original = session.execute(
            select(DocHeader).where(DocHeader.doc_no == doc_no)
        ).scalar_one()
    assert str(original.status) == "reversed"


def test_correct_without_any_document_clarifies_instead_of_building_a_card(
    shop: dict[str, Any],
) -> None:
    """一单都还没记 → 不建「确认更正」卡（建了必 422），转澄清让他补单号。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    shop["forced"]["把上一单更正一下"] = "sys.correct"  # type: ignore[index]

    data = _say(client, headers, "把上一单更正一下")
    print(f"\n[无单可撤] route={data['route']} clarify={data.get('clarify')}")
    assert data["route"] == "clarify", data
    assert "card" not in data
    assert 2 <= len(data["clarify"]["options"]) <= 3


def test_undo_without_any_document_also_clarifies(shop: dict[str, Any]) -> None:
    """同一类坑：``sys.undo`` 在没有任何单据时也不许建注定失败的卡。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    shop["forced"]["撤销上一单"] = "sys.undo"  # type: ignore[index]

    data = _say(client, headers, "撤销上一单")
    assert data["route"] == "clarify", data
    assert "card" not in data
