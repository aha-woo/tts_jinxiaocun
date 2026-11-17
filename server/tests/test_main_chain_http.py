"""主链路回归：**真实 HTTP + PRD 原话**（把 Round 2 的四个 P0 钉在装配层）。

为什么必须走 TestClient 而不用 `t04_fixtures` 造 payload
------------------------------------------------------
Round 2 的四个缺陷（主链路 422、实体写入不可达、报表 500、限流被吞）全都在
**装配层与真实解析链路**上，而 529 条测试没有一条走这条路：

- `tests/t04_fixtures.py` 的 `_SLOT_SAMPLES["S"] = "老张"` 且 `source` 默认全 `"asr"`
  ——「可选槽位留白」这个状态被从测试里抹掉了，所以「`S` 留白 → 确认必失败」测不出来；
- `report.today` / `debt.query` 在 38 个意图里只出现在名单断言里，一次都没执行。

所以本文件的规矩是：**只说 PRD 里那句人话**，其余全部走真 HTTP：
`login → /voice/sessions → /voice/sessions/{id}/parse → /voice/cards/{id}/confirm`，
然后断言库里的**业务终态**（库存数值、商品行、单据状态、报表金额）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import Product, UnitConversion
from app.models.doc import DocHeader
from app.models.inventory import Inventory
from app.tenant.context import (
    TenantContext,
    bind as bind_context,
    reset as reset_context,
)

#: PRD 验收例 ⑦ 的原话（主链路必须能确认）
PURCHASE_UTTERANCE = "进了两箱可乐，一件三十五"
#: 必填缺失的反例（P 槽位是必填，缺了就该拦）
MISSING_REQUIRED_UTTERANCE = "新建商品"
#: 纯实体写入（商品本来还不存在，不能被行级 pending 挡住）
ENTITY_UTTERANCE = "新建商品雪碧"


def _settings() -> Settings:
    """测试配置：内存 SQLite + 固定密钥 + fake 微信。"""
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-main-chain-http-do-not-reuse",
        wx_provider="fake",
    )


@pytest.fixture()
def shop() -> Iterator[dict[str, object]]:
    """一套跑起来的 app + 已登录租户 + 「可乐（瓶/箱=24）」档案。"""
    application = create_app(_settings())
    database = application.state.database
    database.create_all()

    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={"code": "wx-main-chain-1"})
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
                    last_purchase_price_fen=150,
                    last_sale_price_fen=200,
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


def _card_id(data: dict) -> str:
    """卡片 id 在 ``card.meta.card_id``（7 区块 + meta，meta 不参与区块计数）。"""
    card = data["card"]
    return str(card["meta"]["card_id"])


def _card_lines(data: dict) -> list[dict]:
    """明细行在 ``card.calc.lines``。"""
    return list(data["card"]["calc"]["lines"])


def _say(client: TestClient, headers: dict[str, str], text: str) -> dict:
    """说一句话：建会话 → 解析，返回 ``data``。"""
    session = client.post(
        "/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    assert session.status_code == 200, session.text
    session_data = session.json()["data"]
    assert session_data["session_id"], f"会话没建起来：{session_data}"
    parsed = client.post(
        f"/api/v1/voice/sessions/{session_data['session_id']}/parse",
        json={"asr_text": text},
        headers=headers,
    )
    assert parsed.status_code == 200, parsed.text
    return parsed.json()["data"]


def _confirm(client: TestClient, headers: dict[str, str], card_id: str, **extra: object):
    """确认一张卡。"""
    return client.post(
        f"/api/v1/voice/cards/{card_id}/confirm",
        json={"confirm_type": "button", **extra},
        headers=headers,
    )


def _inventory_qty(database, tenant_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    """直接读库存的 base 数量（绕过 scope，看的是真实值）。"""
    with database.session(bypass_scope=True) as session:
        row = session.execute(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id, Inventory.product_id == product_id
            )
        ).scalar_one_or_none()
    return Decimal("0") if row is None else Decimal(row.qty_base)


# ------------------------------------------------ ① 主链路：可选槽位留白不阻塞确认
def test_optional_pending_slot_does_not_block_confirm(shop: dict[str, object]) -> None:
    """PRD 原话「进了两箱可乐，一件三十五」→ **确认必须成功**、库存 0 → 48。

    ``S``（供应商）没说 → ``source["S"] == "pending"`` 是**设计如此**（诚实留白），
    它不在 ``purchase.create`` 的必填里，因此不能拦确认。
    上一版守卫把「可选留白」和「必填缺失」合并，主链路 100% 撞 422。
    """
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]

    data = _say(client, headers, PURCHASE_UTTERANCE)
    assert data["route"] == "card", data
    assert _card_id(data)
    parse = data["parse_result"]
    # 前置事实：S 确实是留白（否则这条用例测不到东西）
    assert parse["source"].get("S") == "pending", parse["source"]

    before = _inventory_qty(database, tenant_id, product_id)
    assert before == Decimal("0")

    response = _confirm(client, headers, _card_id(data), biz_date=None)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0, payload
    body = payload["data"]["result"]
    print(
        f"\n[主链路] card={_card_id(data)} doc_no={body.get('doc_no')} "
        f"total_fen={body.get('total_amount_fen')} source_S={parse['source'].get('S')} "
        f"库存 {before} -> {_inventory_qty(database, tenant_id, product_id)}"
    )
    assert body.get("doc_no"), "确认成功必须返回单号"
    assert _inventory_qty(database, tenant_id, product_id) == Decimal("48"), "2 箱 = 48 瓶"


def test_missing_required_slot_still_blocks_confirm(shop: dict[str, object]) -> None:
    """必填槽位真的缺失时**仍要拦**（守卫收窄的是「可选留白」，不是全部放行）。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]

    data = _say(client, headers, MISSING_REQUIRED_UTTERANCE)
    if data["route"] != "card":
        # 解析阶段就判定缺必填 → 走澄清/手动，同样是「不落账」
        assert data["route"] in {"clarify", "manual", "failed"}, data
        return

    response = _confirm(client, headers, _card_id(data))
    assert response.status_code == 422, response.text
    detail = response.json()["data"]
    assert detail["reason"] == "pending_slots", detail
    assert detail["pending_slots"], "必填缺失必须报出具体槽位"


# ------------------------------------------------ ② 纯实体意图：唯一入口不再被堵死
def test_entity_intent_creates_product_over_http(shop: dict[str, object]) -> None:
    """「新建商品雪碧」→ 确认 200 → `select(Product)` 里真的有「雪碧」。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    database = shop["database"]

    data = _say(client, headers, ENTITY_UTTERANCE)
    assert data["route"] == "card", data
    # 行级 pending 不允许出现在纯实体意图上（商品本来就还不存在）
    assert all(not line.get("pending") for line in _card_lines(data)), data["card"]

    response = _confirm(client, headers, _card_id(data))
    assert response.status_code == 200, response.text
    with database.session(bypass_scope=True) as session:
        created = session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == "雪碧")
        ).scalar_one_or_none()
        names = [
            str(name)
            for name in session.execute(
                select(Product.name).where(Product.tenant_id == tenant_id)
            ).scalars()
        ]
    print(f"\n[实体写入] products={names}")
    assert created is not None, f"「雪碧」没建出来，库里只有 {names}"


# ------------------------------------------------ ③ 撤销：不许「撤销的撤销」

#: ``sys.undo`` 在契约里 ``template_hit=false``（走 LLM），本地 fake LLM 解析不出
#: 「撤销」这类话；所以撤销链路走 **服务层真实调用**（不是伪造 payload），
#: 进货那一步仍然走真 HTTP。这样「反做方向」与「不许撤反做单」两条断言都覆盖得到。

def _reverse(database, context, doc_id: uuid.UUID):
    """在租户上下文里跑一次真实反做。"""
    from app.services.reversal import ReversalService

    token = bind_context(context)
    try:
        with database.session(context=context) as session:
            return ReversalService(session).reverse(
                tenant_id=context.tenant_id,
                store_id=context.store_id,
                doc_id=doc_id,
                member_id=context.member_id,
                reason="测试：反做一次",
            )
    finally:
        reset_context(token)


def _context_of(database, tenant_id: uuid.UUID, headers: dict[str, str]) -> object:
    """用库里第一张单据的 store/created_by 造上下文（不猜 uuid）。"""
    with database.session(bypass_scope=True) as session:
        header = (
            session.execute(select(DocHeader).where(DocHeader.tenant_id == tenant_id))
            .scalars()
            .first()
        )
    assert header is not None, "先落一张单再来造上下文"
    return TenantContext(
        tenant_id=tenant_id,
        store_id=header.store_id,
        member_id=header.created_by,
        role_code="boss",
        tenant_version=1,
    )


def test_reversing_a_reversal_is_rejected(shop: dict[str, object]) -> None:
    """撤销一张**反做单** → ``E_VALIDATION``（reason=cannot_reverse_a_reversal），账不动。

    反做单继承了原单 ``intent_id``，方向只按契约取反 —— 不拦就会再冲销一次
    （QA 实测 240 → 192 → 144）。
    """
    from app.common.errors import AppError

    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    product_id: uuid.UUID = shop["product_id"]  # type: ignore[assignment]
    database = shop["database"]

    purchase = _say(client, headers, PURCHASE_UTTERANCE)
    first = _confirm(client, headers, _card_id(purchase))
    assert first.status_code == 200, first.text
    doc_id = uuid.UUID(first.json()["data"]["result"]["doc_id"])
    assert _inventory_qty(database, tenant_id, product_id) == Decimal("48")

    context = _context_of(database, tenant_id, headers)
    reversal = _reverse(database, context, doc_id)
    after_first = _inventory_qty(database, tenant_id, product_id)
    assert after_first == Decimal("0"), f"反做后应回到 0，实际 {after_first}"

    with pytest.raises(AppError) as excinfo:
        _reverse(database, context, uuid.UUID(str(reversal.reversal_doc_id)))
    assert excinfo.value.code == "E_VALIDATION"
    assert excinfo.value.data["reason"] == "cannot_reverse_a_reversal"
    assert _inventory_qty(database, tenant_id, product_id) == after_first, "被拒的撤销竟然动了账"

    with database.session(bypass_scope=True) as session:
        original = session.execute(
            select(DocHeader).where(DocHeader.doc_id == doc_id)
        ).scalar_one()
        reversal_doc = session.execute(
            select(DocHeader).where(DocHeader.doc_id == uuid.UUID(str(reversal.reversal_doc_id)))
        ).scalar_one()
        print(
            f"\n[撤销终态] 原单={original.doc_no} status={original.status} "
            f"reversed_by={original.reversed_by} | 反做单={reversal_doc.doc_no} "
            f"reverses_doc_id={reversal_doc.reverses_doc_id} | 库存={after_first}"
        )
        assert str(original.status) == "reversed"
        assert reversal_doc.reverses_doc_id == original.doc_id


def test_last_document_lookup_skips_reversal_docs(shop: dict[str, object]) -> None:
    """「上一单」只挑**正常且 confirmed** 的单，不会盯上那辆 ``-R`` 反做单。"""
    from app.services.ledger import LedgerService

    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    database = shop["database"]

    purchase = _say(client, headers, PURCHASE_UTTERANCE)
    first = _confirm(client, headers, _card_id(purchase))
    assert first.status_code == 200, first.text
    doc_id = uuid.UUID(first.json()["data"]["result"]["doc_id"])
    context = _context_of(database, tenant_id, headers)

    token = bind_context(context)
    try:
        with database.session(context=context) as session:
            resolved_before = LedgerService(session)._resolve_target_doc(
                tenant_id=tenant_id, raw_ref="上一单"
            )
            assert resolved_before is not None and resolved_before.doc_id == doc_id
    finally:
        reset_context(token)

    _reverse(database, context, doc_id)

    token = bind_context(context)
    try:
        with database.session(context=context) as session:
            resolved_after = LedgerService(session)._resolve_target_doc(
                tenant_id=tenant_id, raw_ref="上一单"
            )
    finally:
        reset_context(token)
    print(f"\n[上一单] 撤销后 -> {None if resolved_after is None else resolved_after.doc_no}")
    assert resolved_after is None, (
        "撤销后「上一单」不该指向反做单（否则用户再说一句撤销就再冲销一次）"
    )


# ------------------------------------------------ ④ 报表不再 500
@pytest.mark.parametrize("utterance", ["今天卖了多少", "老张欠我多少"])
def test_report_and_debt_intents_return_200(shop: dict[str, object], utterance: str) -> None:
    """「今天卖了多少」「老张欠我多少」→ 200（不是 500 的 AttributeError）。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]

    data = _say(client, headers, utterance)
    print(f"\n[报表] {utterance} -> route={data['route']} answer={data.get('answer')}")
    assert data["route"] in {"answer", "clarify", "manual"}, data


def test_reports_exclude_reversal_docs(shop: dict[str, object]) -> None:
    """撤销后「今天采购多少」必须归零（反做单不许被当成又一笔采购）。"""
    from datetime import date

    from app.domain.report_service import ReportService

    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]
    tenant_id: uuid.UUID = shop["tenant_id"]  # type: ignore[assignment]
    database = shop["database"]

    purchase = _say(client, headers, PURCHASE_UTTERANCE)
    first = _confirm(client, headers, _card_id(purchase))
    assert first.status_code == 200, first.text
    doc_id = uuid.UUID(first.json()["data"]["result"]["doc_id"])
    context = _context_of(database, tenant_id, headers)

    def _purchase_total() -> int:
        token = bind_context(context)
        try:
            with database.session(context=context) as session:
                report = ReportService(session).today(
                    tenant_id=tenant_id, store_id=context.store_id, biz_date=date.today()
                )
                return int(report.purchase_fen)
        finally:
            reset_context(token)

    before = _purchase_total()
    assert before == 7000, f"进货 2×3500=7000，实际 {before}"

    _reverse(database, context, doc_id)

    after = _purchase_total()
    print(f"\n[报表] 今日采购额 撤销前={before} 撤销后={after}")
    assert after == 0, "撤销后今日采购额必须归零（反做单不能被当采购）"


# ------------------------------------------------ ⑤ 分钟限流在 API 层真的生效
def test_minute_rate_limit_is_enforced_over_http(shop: dict[str, object]) -> None:
    """同一租户连打 35 次 ``POST /voice/sessions`` → 放行 30、拒绝 5。"""
    client: TestClient = shop["client"]  # type: ignore[assignment]
    headers: dict[str, str] = shop["headers"]  # type: ignore[assignment]

    allowed = 0
    rejected = 0
    reasons: set[str] = set()
    for _ in range(35):
        response = client.post(
            "/api/v1/voice/sessions",
            json={"duration_ms": 2_000, "energy_rms": 0.5},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        if data.get("session_id"):
            allowed += 1
        else:
            rejected += 1
            reasons.add(str(data.get("reason") or data.get("error_code")))

    print(f"\n[限流] allowed={allowed} rejected={rejected} reasons={sorted(reasons)}")
    assert allowed == 30, f"一分钟上限 30，实际放行 {allowed}"
    assert rejected == 5, f"应拒绝 5 次，实际 {rejected}"
    assert "rate_limited" in reasons
