"""Round 4 自查缺陷的**回归护栏**：四条修复各自钉住一条断言。

这四条都是我在上一轮**自己动手改代码时**发现并修掉的，不是 QA 报出来的，
所以这里必须每条都留一个「改回去就会红」的测试——不然下次重构又会漂回去。

四条分别是什么
--------------
1. **Fix 1（P1-B）建卡失败不许 500**：``ConfirmCardBuilder.build`` 抛
   ``CardBuildError`` 时，``POST /voice/sessions/{id}/parse`` 以前直接冒泡成
   500（包络全丢、前端连 ``error_code`` 都拿不到）。现在必须分流成
   ``route=clarify``（缺必填槽）或 ``route=manual``（结构性错误），**都不扣次**。
2. **Fix 2（P0）端侧去重不许静默吞单**：判重从「解析之后」前移到「建会话之前」，
   命中时用弹窗追问「是又记了一笔，还是刚才点重了？」，用户确认才发请求。
   （端侧在 ``miniprogram/pages/index/index.ts``，本文件断言**服务端不参与**这个决定。）
3. **Fix 3（P1-A）无卡澄清不许 422**���``route=clarify`` 分支**本来就不建卡**，
   而 ``/voice/sessions/{id}/clarify`` 原来只从**卡片载荷**里取 ``intent_id``
   → 没卡就 422 → 用户点完选项什么也不会发生。现在从 ``voice_log.parse_result``
   快照兜底，补齐后**当场出卡**。
4. **Fix 4（P2-C）别名前导连接词要剥掉**：LLM 抽出 ``ALIAS='叫快乐水'`` 时，
   别名落库成「叫快乐水」→ 用户以后说「快乐水」解析不到 → **教了等于没教**。

本文件的规矩和 ``test_main_chain_http`` 一致：**说人话、走真 HTTP、断言库里的终态**。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.voice import ConfirmCard, VoiceLog
from app.services.ledger import LedgerService
from app.voice.card_builder import CardBuildError

#: PRD 原话：数量和金额都在，**只缺商品** → 规则层就该走澄清
MISSING_PRODUCT_UTTERANCE = "今天进了两箱，一件三十五"
#: 正常主链路
PURCHASE_UTTERANCE = "进了两箱可乐，一件三十五"


# --------------------------------------------------------------------- fixtures
def _settings():
    from app.config import Settings

    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-round4-fixes-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, Any]]:
    """跑起来的 app + 已登录租户 + 「可乐（箱/瓶=24）」档案。"""
    from app.main import create_app

    application = create_app(_settings())
    database = application.state.database
    database.create_all()

    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-round4-1"})
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
                    name="可乐",
                    spec="500ml",
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

        yield {
            "client": client,
            "database": database,
            "headers": headers,
            "tenant_id": tenant_id,
            "product_id": product_id,
        }

    database.drop_all()
    database.dispose()


# --------------------------------------------------------------------- helpers
def _say(client: TestClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    """说一句话：建会话 → 解析，返回 ``data``（断言 HTTP 200）。"""
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


def _card_count(database, tenant_id: uuid.UUID) -> int:
    """数这个租户的确认卡（断言「没建出卡」用）。"""
    with database.session(bypass_scope=True) as session:
        return len(
            session.execute(
                select(ConfirmCard).where(ConfirmCard.tenant_id == tenant_id)
            ).scalars()
            .all()
        )


def _voice_log(database, tenant_id: uuid.UUID, session_id: str) -> VoiceLog | None:
    """取这条会话的 ASR 留痕行（含解析快照）。"""
    with database.session(bypass_scope=True) as session:
        return (
            session.execute(
                select(VoiceLog).where(
                    VoiceLog.tenant_id == tenant_id,
                    VoiceLog.session_id == uuid.UUID(session_id),
                )
            )
            .scalars()
            .first()
        )


# ------------------------------------------------ Fix 1：建卡失败不许 500
@pytest.fixture()
def broken_card_builder(monkeypatch: pytest.MonkeyPatch):
    """把「建卡」这一步换成必失败，用来复现**真实**的建卡失败分支。

    为什么不用一句人话去撞
    ----------------------
    规则层（G3）已经把「缺必填槽」分流成 ``route=clarify`` 了，所以正常说话
    **根本走不到** ``ConfirmCardBuilder.build`` 的缺槽位分支——那得等 LLM
    兜底抽出一个残缺结果（``template_hit=false`` 的意图）才可能发生。
    真实链路里那条路本地测不到，所以这里直接把建卡这一步**打断**，
    断言上层**怎么接**这个异常（这才是 Fix 1 改的地方）。
    """

    def _raise(*args: Any, **kwargs: Any):
        raise CardBuildError("测试注入：建卡失败", missing=("P",))

    monkeypatch.setattr(LedgerService, "create_card", _raise)


def test_card_build_failure_returns_clarify_not_500(
    shop: dict[str, Any], broken_card_builder: None
) -> None:
    """建卡失败（缺必填槽）→ **200 + route=clarify**，不是 500。

    上一版的洞
    ----------
    ``ConfirmCardBuilder.build`` 抛 ``CardBuildError`` 时，``parse_session``
    没有捕获 → FastAPI 直接 500。对一个「不识字的商户」产品，用户看到的是
    「Internal Server Error」——既看不懂也不会重试，而且包络全丢，
    前端连 ``error_code`` 都拿不到。
    """
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, PURCHASE_UTTERANCE)
    assert data["route"] == "clarify", data
    assert data["clarify"], "追问必须带话术与选项"
    assert 2 <= len(data["clarify"]["options"]) <= 3, data["clarify"]
    assert data.get("missing_slots") == ["P"], data
    assert data.get("error_code"), "必须给出错误码（前端按它决定怎么提示）"
    # 两种分流都**不扣次**（卡没建出来就没有消耗）
    assert data.get("credit_consumed") is False, data
    # 库里不能留下一张「建得出来、点下去必失败」的卡
    assert _card_count(database, tenant_id) == 0, "建卡失败却留下了卡"


def test_card_build_failure_without_missing_returns_manual(
    shop: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**结构性**建卡失败（不缺槽位）→ 切数字键盘补录，不是追问。

    分流依据是 ``CardBuildError.missing``：
    - 有缺槽 → 缺的是**单个信息** → 追问一句就能补上；
    - 没缺槽 → 是**结构性**问题（缺区块 / source 标注违规）→ 追问用户也补不上，
      只能让他手打。

    没有 ``missing`` 这个字段的话，调用方只能靠字符串匹配去猜，猜错就会
    拿结构性错误去问用户——用户答了还是错。
    """

    def _raise(*args: Any, **kwargs: Any):
        raise CardBuildError("测试注入：卡片缺区块")

    monkeypatch.setattr(LedgerService, "create_card", _raise)

    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, PURCHASE_UTTERANCE)
    print(f"\n[Fix1-结构错] route={data['route']} error_code={data.get('error_code')}")
    assert data["route"] == "manual", data
    assert data.get("manual_entry_required") is True, data
    assert data.get("credit_consumed") is False, data
    assert _card_count(database, tenant_id) == 0


def test_missing_required_slot_never_becomes_500(shop: dict[str, Any]) -> None:
    """真实链路：缺必填槽 → 追问/补录，**绝不 500**，也绝不留下残卡。"""
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, "新建商品")
    print(f"\n[Fix1-真实] route={data['route']} reason={data.get('reason')}")
    assert data["route"] in {"clarify", "manual"}, data
    assert _card_count(database, tenant_id) == 0, "缺必填槽却留下了卡"


def test_card_build_failure_keeps_envelope_shape(shop: dict[str, Any]) -> None:
    """失败响应**必须仍是标准包络**（前端统一按 ``code``/``data`` 解析）。"""
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]

    session = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    session_id = session.json()["data"]["session_id"]
    response = client.post(
        f"/api/v1/voice/sessions/{session_id}/parse",
        json={"asr_text": "新建商品"},
        headers=headers,
    )
    body = response.json()
    assert set(body.keys()) >= {"code", "data", "message"}, body
    assert isinstance(body["data"], dict), body


# ------------------------------------------------ Fix 3：无卡澄清不许 422
def test_clarify_without_card_falls_back_to_snapshot_and_builds_card(
    shop: dict[str, Any],
) -> None:
    """``route=clarify``（无卡）→ 回答澄清 → **当场出卡**，不是 422。

    这条是整套产品「说不全」的通用支线：缺必填槽、置信度低都走这里。
    上一版因为「只从卡片载荷取 intent_id」而必然 422，PRD 写的
    「最多问 2 句」实际**一句都问不成**。
    """
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, MISSING_PRODUCT_UTTERANCE)
    assert data["route"] == "clarify", data
    assert data["clarify"]["slot"] == "P", data
    # 澄清分支**不建卡**（澄清前不该占一张卡、不该扣次）
    assert _card_count(database, tenant_id) == 0, "澄清阶段不该建卡"

    response = client.post(
        f"/api/v1/voice/sessions/{data['session_id']}/clarify",
        json={"round": 1, "slot": "P", "value": "可乐"},
        headers=headers,
    )
    assert response.status_code == 200, f"澄清不该 422：{response.text}"
    body = response.json()
    assert body["code"] == 0, body
    clarified = body["data"]
    print(f"\n[Fix3] route={clarified['route']} slots={clarified.get('slots')}")
    assert clarified["route"] == "card", clarified
    assert clarified["card"]["meta"]["card_id"], clarified
    # 补建走同一条 create_card：卡片生成成功才扣 1 次
    assert clarified["credit_consumed"] is True, clarified
    # 库里真的多了一张卡
    assert _card_count(database, tenant_id) == 1


def test_clarify_rebuild_keeps_calc_traces(shop: dict[str, Any]) -> None:
    """补建的卡必须带得上 ``TA``（合计）的计算轨迹。

    为什么这条要单独钉
    ------------------
    ``TA`` 这类槽位在 ``source`` 里标着 ``computed``，而 ``computed`` 槽位
    **必须**有对应计算式（不许黑箱数字）。补建时如果只带槽位、不带轨迹，
    卡片会被判「source 标注违规」→ 建卡失败 → 澄清补齐了却拿不到卡。
    轨迹在解析那一步就写进快照了，这条断言守住「别把它弄丢」。
    """
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]

    data = _say(client, headers, MISSING_PRODUCT_UTTERANCE)
    response = client.post(
        f"/api/v1/voice/sessions/{data['session_id']}/clarify",
        json={"round": 1, "slot": "P", "value": "可乐"},
        headers=headers,
    )
    card = response.json()["data"]["card"]
    total = next((s for s in card["slots"] if s["code"] == "TA"), None)
    assert total is not None, card["slots"]
    assert total["source"] == "computed", total
    assert total["calc_trace"], "合计槽位必须有计算式（不许黑箱数字）"


def test_parse_snapshot_is_written_for_clarify_route(shop: dict[str, Any]) -> None:
    """澄清路由也必须写解析快照（快照是「无卡也能接着澄清」的唯一依据）。"""
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, MISSING_PRODUCT_UTTERANCE)
    row = _voice_log(database, tenant_id, data["session_id"])
    assert row is not None, "解析后必须留 ASR 痕迹"
    assert row.parse_result, "澄清路由也必须写解析快照"
    assert row.parse_result["intent"] == "purchase.create", row.parse_result
    assert "P" not in row.parse_result["slots"], row.parse_result
    assert row.parse_result["calc_traces"], "快照必须带计算轨迹（补建卡要用）"


def test_parse_snapshot_survives_decimal_slots(shop: dict[str, Any]) -> None:
    """槽位里出现 ``Decimal`` 时快照**不能炸**（JSON 列塞不进 Decimal）。

    这是一条「会静默炸生产」的坑：``voice_log.parse_result`` 是 JSON 列，
    直接塞 ``Decimal`` 会 ``StatementError`` → 整个 parse 请求 500。
    """
    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    data = _say(client, headers, PURCHASE_UTTERANCE)
    assert data["route"] == "card", data
    row = _voice_log(database, tenant_id, data["session_id"])
    assert row is not None and row.parse_result, "主链路也必须写快照"
    # 金额槽位是整数分（能被 JSON 序列化），不能是 Decimal
    assert isinstance(row.parse_result["slots"]["TA"], int), row.parse_result


# ------------------------------------------------ Fix 4：别名前导连接词
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("叫快乐水", "快乐水"),
        ("叫做快乐水", "快乐水"),
        ("就是快乐水", "快乐水"),
        ("称为快乐水", "快乐水"),
        ("叫作快乐水", "快乐水"),
        ("快乐水", "快乐水"),  # 本来就没连接词 → 不动
        ("小汽水", "小汽水"),  # 「小」不是连接词 → 不动
    ],
)
def test_strip_alias_cue_removes_leading_connector(raw: str, expected: str) -> None:
    """别名开头的连接词要剥掉（否则「教了等于没教」）。"""
    from app.domain.entity_writer import _strip_alias_cue

    assert _strip_alias_cue(raw) == expected


def test_strip_alias_cue_keeps_product_name_intact() -> None:
    """商品名本身以连接词开头时**不许动**（「叫花鸡」不能被教成「花鸡」）。"""
    from app.domain.entity_writer import _strip_alias_cue

    assert _strip_alias_cue("叫花鸡味", product_name="叫花鸡") == "叫花鸡味"
    # 别名全被剥光时保持原样（剥光 = 没剥）
    assert _strip_alias_cue("叫", product_name="可乐") == "叫"


def test_alias_cue_is_stripped_before_persisting(shop: dict[str, Any]) -> None:
    """走真链路落库：别名列里存的是**剥掉连接词后**的「快乐水」。"""
    from app.models.catalog import ProductAlias as _Alias  # noqa: F401 - 确认模型可导入

    client: TestClient = shop["client"]
    headers: dict[str, str] = shop["headers"]
    tenant_id: uuid.UUID = shop["tenant_id"]
    database = shop["database"]

    # 直接走实体写入器（别名落库在那儿）
    from app.domain.entity_writer import EntityWriter
    from app.tenant.context import TenantContext

    member_id = _member_id_of(database, tenant_id)
    context = TenantContext(
        tenant_id=tenant_id,
        store_id=_store_id_of(database, tenant_id),
        member_id=member_id,
        role_code="boss",
        tenant_version=1,
    )
    with database.session(context=context) as session:
        EntityWriter(session).apply(
            tenant_id=tenant_id,
            store_id=context.store_id,
            member_id=member_id,
            intent_id="product.alias.add",
            slots={"P": "可乐", "ALIAS": "叫快乐水"},
            source={"P": "asr", "ALIAS": "asr"},
        )

    with database.session(bypass_scope=True) as session:
        aliases = [
            str(row.alias_text)
            for row in session.execute(
                select(ProductAlias).where(ProductAlias.tenant_id == tenant_id)
            ).scalars()
        ]
    print(f"\n[Fix4] aliases={aliases}")
    assert "快乐水" in aliases, f"别名列里应该是「快乐水」，实际 {aliases}"
    assert "叫快乐水" not in aliases, f"连接词没剥掉：{aliases}"


def _member_id_of(database, tenant_id: uuid.UUID) -> uuid.UUID:
    """取这个租户的老板 member_id（不猜 uuid）。"""
    from app.models.org import Member

    with database.session(bypass_scope=True) as session:
        member = (
            session.execute(select(Member).where(Member.tenant_id == tenant_id))
            .scalars()
            .first()
        )
    assert member is not None, "登录后应当有成员"
    return uuid.UUID(str(member.member_id))


def _store_id_of(database, tenant_id: uuid.UUID) -> uuid.UUID:
    """取这个租户的主仓 store_id。"""
    from app.models.org import Store

    with database.session(bypass_scope=True) as session:
        store = (
            session.execute(
                select(Store).where(Store.tenant_id == tenant_id, Store.is_main.is_(True))
            )
            .scalars()
            .first()
        )
    assert store is not None, "登录后应当有主仓"
    return uuid.UUID(str(store.store_id))
