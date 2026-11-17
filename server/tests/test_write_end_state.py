"""写意图的**业务终态**断言（不是「有单据 / 有卡片 / 扣了次」）。

为什么单开一个文件
------------------
T04/T05 的 507 条断言验的是「有单据 / 有卡片 / 扣了次」。这些断言全绿的同时，
下面这些事一件都没被挡住：

- ``sys.undo`` 确认后**没撤销**，反而多写一张正向单（账面多出一笔采购）；
- ``product.create`` 确认后商品档案里**没有这个商品**（只多了一条零金额单据）；
- 卡片上明明标着「待确认」，确认时照样落账（5 箱记成 5 瓶）。

所以本文件的断言一律看**库里的终态**：商品到底建出来没有、库存回没回退、
原单状态是不是 reversed、有没有多出不该有的单据。
这类断言不会因为「链路长得像成功了」而放过。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.common.errors import AppError
from app.domain.entity_writer import EntityWriter, UnsupportedSideEffect
from app.models.catalog import Product, ProductAlias
from app.models.doc import DocHeader
from app.models.partner import Partner
from t04_fixtures import Seed, count_rows, create_card_for, lines_for, seed_shop

#: 种子里的主仓初始库存（瓶）
SEED_QTY = Decimal("192")


@pytest.fixture()
def shop() -> Seed:
    """装好种子数据的内存库（用完释放连接池）。"""
    seeded = seed_shop()
    try:
        yield seeded
    finally:
        seeded.database.drop_all()
        seeded.database.dispose()


def _confirm(
    seeded: Seed,
    *,
    intent_id: str,
    slots: dict | None = None,
    source: dict | None = None,
    lines: list | None = None,
    confirm_type: str = "button",
    spoken_text: str | None = None,
    with_product: bool = True,
):
    """建卡 + 确认（同一事务），返回（卡片创建结果, 确认结果）。"""
    from app.services.ledger import LedgerService

    with seeded.database.session(context=seeded.context) as session:
        ledger, creation = create_card_for(
            seeded,
            session=session,
            intent_id=intent_id,
            slots=slots,
            source=source,
            lines=(
                lines
                if lines is not None
                else lines_for(seeded, **({} if with_product else {"with_product": False}))
            ),
        )
        outcome = ledger.confirm(
            tenant_id=seeded.tenant_id,
            store_id=seeded.store_id,
            member_id=seeded.member_id,
            card_id=uuid.UUID(creation.card_id),
            confirm_type=confirm_type,
            spoken_text=spoken_text,
            button_fallback=confirm_type == "button",
        )
    return creation, outcome


def _product_named(seeded: Seed, name: str) -> Product | None:
    """按名字查商品（**新会话**，确保读到的是真落库的数据）。"""
    with seeded.database.session(context=seeded.context) as session:
        return session.execute(
            select(Product).where(Product.tenant_id == seeded.tenant_id, Product.name == name)
        ).scalar_one_or_none()


def _qty_of_seed_product(seeded: Seed) -> Decimal:
    """读种子商品在当前门店的库存（瓶）。"""
    from app.domain.inventory_service import InventoryService

    with seeded.database.session(context=seeded.context) as session:
        row = InventoryService(session).get_qty_base(
            tenant_id=seeded.tenant_id, store_id=seeded.store_id, product_id=seeded.product_id
        )
        return Decimal(str(row.qty_base)) if row is not None else Decimal("0")


# ---------------------------------------------------------------- P0-1 撤销
def test_sys_undo_reverses_original_and_adds_no_positive_doc(shop: Seed) -> None:
    """``sys.undo`` 必须**反做**：库存回退、原单 reversed、不新增正向单。"""
    _, purchase = _confirm(shop, intent_id="purchase.create")
    assert purchase.confirmed
    assert _qty_of_seed_product(shop) == SEED_QTY + Decimal("48")
    docs_after_purchase = count_rows(shop.database, DocHeader)

    _, undo = _confirm(shop, intent_id="sys.undo", confirm_type="voice2", spoken_text="确认")

    assert undo.confirmed, undo.as_dict()
    assert _qty_of_seed_product(shop) == SEED_QTY, "撤销后库存没回退"
    with shop.database.session(context=shop.context) as session:
        original = session.execute(
            select(DocHeader).where(DocHeader.doc_no == str(purchase.doc_no))
        ).scalar_one()
        assert str(original.status) == "reversed", "原单没被标记为已撤销"
        assert original.reversed_by is not None
    # 只多了一张**反做单**，且它继承了原单的金额（不是一笔新的 +¥70）
    assert count_rows(shop.database, DocHeader) == docs_after_purchase + 1
    assert undo.reversal_doc_no and undo.reversal_doc_no != purchase.doc_no


def test_sys_correct_reverses_first_then_asks_for_rerecord(shop: Seed) -> None:
    """``sys.correct`` = 反做 + 重记：**第一步必须真的做成**，且不猜新金额。

    上一版这里直接抛 ``E_VALIDATION`` —— 卡建出来了、确认 100% 失败。
    反做本来是可执行的第一步（契约 ``name_zh`` 写的就是「反做 + 重记」），
    所以正确行为是：原单 reversed、生成反做单、回执里带 ``follow_up=rerecord``；
    **不额外写一张「更正单」**（那需要新明细，猜出来就是一笔用户没确认过的账）。
    """
    _, purchase = _confirm(shop, intent_id="purchase.create")
    docs_after_purchase = count_rows(shop.database, DocHeader)
    qty_after_purchase = _qty_of_seed_product(shop)

    _, correct = _confirm(shop, intent_id="sys.correct", confirm_type="voice2", spoken_text="确认")

    assert correct.confirmed, correct.as_dict()
    # ① 反做真的发生了：库存回退 + 原单 reversed
    assert qty_after_purchase == SEED_QTY + Decimal("48")
    assert _qty_of_seed_product(shop) == SEED_QTY, (
        f"反做后库存应回到 {SEED_QTY}，实际 {_qty_of_seed_product(shop)}"
    )
    with shop.database.session(context=shop.context) as session:
        original = session.execute(
            select(DocHeader).where(DocHeader.doc_no == str(purchase.doc_no))
        ).scalar_one()
        assert str(original.status) == "reversed", "更正第一步没把原单标记为已撤销"
    # ② 只多了**一张反做单**，没有凭 TV 猜出来的「更正单」
    assert count_rows(shop.database, DocHeader) == docs_after_purchase + 1
    # ③ 回执明确告诉用户「还有第二步：重说一句」
    assert correct.follow_up is not None, "没有下一步指引，用户以为已经改好了"
    assert correct.follow_up["action"] == "rerecord"
    assert correct.follow_up["step"] == 2
    assert str(purchase.doc_no) in str(correct.follow_up["hint_zh"])


# ---------------------------------------------------------------- P0-2 实体
def test_product_create_really_creates_a_product_row(shop: Seed) -> None:
    """``product.create`` 确认后，商品档案里必须**真的有**这个商品。"""
    _, outcome = _confirm(shop, intent_id="product.create", slots={"P": "雪碧"})

    assert outcome.confirmed, outcome.as_dict()
    assert outcome.entity == "product"
    created = _product_named(shop, "雪碧")
    assert created is not None, "只写了单据，商品档案里没有「雪碧」"
    assert str(created.status) == "active"


def test_product_create_twice_does_not_duplicate(shop: Seed) -> None:
    """同一句话确认两次（复活再确认）不得产生两条同名商品。"""
    _confirm(shop, intent_id="product.create", slots={"P": "雪碧"})
    _confirm(shop, intent_id="product.create", slots={"P": "雪碧"})

    with shop.database.session(context=shop.context) as session:
        rows = session.execute(
            select(Product).where(Product.tenant_id == shop.tenant_id, Product.name == "雪碧")
        ).scalars().all()
    assert len(rows) == 1, f"同名商品建了 {len(rows)} 条"


def test_price_update_changes_last_price_not_history(shop: Seed) -> None:
    """``price.update`` 改的是商品的默认价（历史单据不回溯）。"""
    old = _product_named(shop, "可乐")
    assert old is not None

    _, outcome = _confirm(
        shop, intent_id="price.update", slots={"P": "可乐", "TF": "进价", "TV": "3块8"}
    )

    assert outcome.confirmed, outcome.as_dict()
    updated = _product_named(shop, "可乐")
    assert updated is not None
    assert int(updated.last_purchase_price_fen) == 380, "价格没真正改到档案上"


def test_product_alias_add_writes_alias_row(shop: Seed) -> None:
    """``product.alias.add`` 必须落到 ``product_alias``（否则下一句还是听不懂）。"""
    _, outcome = _confirm(
        shop, intent_id="product.alias.add", slots={"P": "可乐", "ALIAS": "快乐水"}
    )

    assert outcome.confirmed, outcome.as_dict()
    with shop.database.session(context=shop.context) as session:
        alias = session.execute(
            select(ProductAlias).where(
                ProductAlias.tenant_id == shop.tenant_id,
                ProductAlias.alias_text == "快乐水",
            )
        ).scalar_one_or_none()
    assert alias is not None, "别名没落到 product_alias"
    assert alias.product_id == shop.product_id


def test_product_disable_sets_status(shop: Seed) -> None:
    """``product.disable`` 是停用（非删除）：状态变 disabled，行还在。"""
    _, outcome = _confirm(shop, intent_id="product.disable", slots={"P": "可乐"})

    assert outcome.confirmed, outcome.as_dict()
    disabled = _product_named(shop, "可乐")
    assert disabled is not None, "停用把商品删了（历史单据会指向空气）"
    assert str(disabled.status) == "disabled"


def test_partner_create_writes_partner_row(shop: Seed) -> None:
    """``partner.create`` 必须落到 ``partner`` 表。"""
    _, outcome = _confirm(
        shop,
        intent_id="partner.create",
        slots={"TF": "名称", "TV": "兴旺超市", "C": "兴旺超市"},
        with_product=False,
    )

    assert outcome.confirmed, outcome.as_dict()
    with shop.database.session(context=shop.context) as session:
        partner = session.execute(
            select(Partner).where(
                Partner.tenant_id == shop.tenant_id, Partner.name == "兴旺超市"
            )
        ).scalar_one_or_none()
    assert partner is not None, "伙伴档案里没有「兴旺超市」"
    assert str(partner.type) == "customer"


def test_entity_writer_refuses_unimplemented_intent() -> None:
    """没实现的 side_effect 必须炸（不允许静默写空单）。"""
    writer = EntityWriter(session=None)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedSideEffect) as excinfo:
        writer.apply(
            intent_id="purchase.create",
            tenant_id=uuid.uuid4(),
            store_id=uuid.uuid4(),
            member_id=uuid.uuid4(),
            slots={"P": "可乐"},
        )
    assert excinfo.value.intent_id == "purchase.create"


# ---------------------------------------------------------------- P0-4 pending
def test_pending_line_cannot_be_confirmed(shop: Seed) -> None:
    """行级 ``pending=True``（缺换算系数）→ 确认必须被拒，不静默落账。"""
    docs_before = count_rows(shop.database, DocHeader)

    with pytest.raises(AppError) as excinfo:
        _confirm(shop, intent_id="purchase.create", lines=lines_for(shop, pending=True))

    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert "待确认" in excinfo.value.resolved_message
    assert count_rows(shop.database, DocHeader) == docs_before, "被拒了还写了单"
    assert _qty_of_seed_product(shop) == SEED_QTY


def test_pending_slot_cannot_be_confirmed(shop: Seed) -> None:
    """槽位级 ``source='pending'`` → 确认必须被拒。"""
    with pytest.raises(AppError) as excinfo:
        _confirm(shop, intent_id="purchase.create", source={"P": "pending"})

    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert "待确认" in excinfo.value.resolved_message


# ---------------------------------------------------------------- P1-12 复活
def test_revived_card_view_carries_the_real_card_id(shop: Seed) -> None:
    """同一 session 两次建卡：第二次返回的 ``card_id`` 必须能拿去确认。"""
    from app.services.ledger import LedgerService
    from t04_fixtures import parse_of, preview_for

    session_id = uuid.uuid4()
    with shop.database.session(context=shop.context) as session:
        ledger = LedgerService(session)
        rows = lines_for(shop)
        parse = parse_of("purchase.create")
        preview = preview_for(shop, session=session, lines=rows)
        first = ledger.create_card(
            tenant_id=shop.tenant_id,
            store_id=shop.store_id,
            member_id=shop.member_id,
            session_id=session_id,
            parse=parse,
            asr_text="进了两箱可乐",
            asr_confidence=0.95,
            lines=rows,
            preview=preview,
        )
        second = ledger.create_card(
            tenant_id=shop.tenant_id,
            store_id=shop.store_id,
            member_id=shop.member_id,
            session_id=session_id,
            parse=parse,
            asr_text="进了两箱可乐",
            asr_confidence=0.95,
            lines=rows,
            preview=preview,
        )

        # 视图里的 card_id 必须与「真正存在的卡片」一致
        assert first.view.card_id == first.card_id
        assert second.view.card_id == first.card_id, "复活后的视图带着新 uuid，拿它确认必 422"
        assert second.card_id == first.card_id

        outcome = ledger.confirm(
            tenant_id=shop.tenant_id,
            store_id=shop.store_id,
            member_id=shop.member_id,
            card_id=uuid.UUID(second.view.card_id),
            confirm_type="button",
            button_fallback=True,
        )
    assert outcome.confirmed, outcome.as_dict()


# ---------------------------------------------------------------- P1-6 同商品多行
def test_two_lines_of_same_product_confirm_without_version_conflict(shop: Seed) -> None:
    """「两箱可乐，三箱可乐」：同一门店同一商品两行 → 合并成一次 CAS，**不能永久 1202**。"""
    rows = lines_for(shop, quantity=2, qty_base=48) + [
        {**lines_for(shop, quantity=3, qty_base=72)[0]}
    ]
    _, outcome = _confirm(shop, intent_id="purchase.create", lines=rows)

    assert outcome.confirmed, outcome.as_dict()
    assert _qty_of_seed_product(shop) == SEED_QTY + Decimal("48") + Decimal("72")


# ---------------------------------------------------------------- P1-5 缺换算系数
def test_missing_unit_ratio_marks_line_pending() -> None:
    """缺「1 箱=?瓶」时，``_qty_base_of`` 必须报告「未解析」，由调用方标 pending。"""
    from app.services.voice_runtime import _qty_base_of

    class _Product:
        base_unit = "瓶"
        ratios: dict = {}

    qty_base, resolved = _qty_base_of(_Product(), 5, "箱")
    assert qty_base == Decimal("5")  # 不臆造系数
    assert resolved is False, "缺系数却报告已解析 → 5 箱会被当成 5 瓶落账"

    # 同上，说的就是基本单位时不需要系数，视为已解析
    qty_base, resolved = _qty_base_of(_Product(), 5, "瓶")
    assert (qty_base, resolved) == (Decimal("5"), True)


# ---------------------------------------------------------------- P1-8 负数
def test_negative_marker_blocks_write_intent() -> None:
    """「进了负两箱可乐」必须被 C7 拦住（数字解析会把符号丢掉）。"""
    from app.voice.conflict_detector import ConflictContext, detect

    conflicts = detect(
        ConflictContext(
            text="进了负两箱可乐",
            intent_id="purchase.create",
            slots={"P": "可乐", "Q": 2, "U": "箱"},
            source={},
        )
    )
    codes = {conflict.code for conflict in conflicts}
    assert "C7" in codes, f"负数标记没被拦住，conflicts={sorted(codes)}"
    assert any(conflict.code == "C7" and conflict.blocked for conflict in conflicts)
