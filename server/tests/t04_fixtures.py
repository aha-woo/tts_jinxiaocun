"""T04 共享夹具：一个装好种子数据的内存库 + 造卡片/预演的辅助函数。

为什么集中在这里
----------------
T04 的每个测试都要「34 张表 + 一个租户 + 一个主仓 + 一个商品 + 一条库存 +
一张卡片」，各写一份必然出现「这个文件建了 3 张表、那个文件忘了建」的漂移。
所以：建库、装种子、造 ``ParseResult``、算预演、建卡片**只在本文具里实现一次**。

两条纪律
--------
1. **种子数据必须 commit**（``Database.session`` 退出即提交）：只读预演会另开/复用
   会话并在结束时 ``rollback``，SQLite 内存库全进程共用一条连接，
   未提交的写会被那次 rollback 一起带走——这不是 bug，是内存库的物理事实；
2. **槽位值只从契约推**：:func:`slots_for` 按 ``required_slots`` 生成最小合法值，
   测试不硬编码「purchase.create 要哪几个槽位」（契约改了不用改测试）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import pytest
from sqlalchemy import select

import app.models  # noqa: F401  确保 34 张表全部注册到 metadata
from app.config import get_contracts
from app.db.base import Base
from app.db.session import Database
from app.domain.intent_registry import IntentSpec, default_registry
from app.models.catalog import Product, UnitConversion
from app.models.inventory import Inventory
from app.models.org import Member, Role, Store
from app.models.partner import Partner
from app.models.tenant import Tenant
from app.services.ledger import CardCreation, LedgerService
from app.tenant.context import TenantContext, clear
from app.utils.money import yuan_to_fen
from app.voice.impact_preview import ImpactPreviewService, PreviewBundle, build_deltas
from app.voice.output_schema import ParseResult

__all__ = [
    "DEFAULT_INTENT",
    "Seed",
    "create_card_for",
    "dump_snapshot",
    "lines_for",
    "new_database",
    "parse_of",
    "preview_for",
    "qty_base_of",
    "seed_shop",
    "slots_for",
]

#: 造卡片默认用的意图（R1 + C1，最省事的「真能落账」样例）
DEFAULT_INTENT = "purchase.create"

#: 槽位样例值（只用于「凑够必填槽位」，不参与任何业务断言）
_SLOT_SAMPLES: Mapping[str, Any] = {
    "P": "可乐",
    "Q": 2,
    "U": "箱",
    "UP": 3500,
    "TA": 7000,
    "AMT": 7000,
    "D": "2026-02-01",
    "PS": "已结",
    "C": "散客",
    "S": "老张",
    "W": "主仓",
    "N": "没拆封",
    "DOC": "上一单",
    "TF": "进价",
    "TV": "3块5",
    "EF": "2026-02-01",
    "ALIAS": "快乐水",
    "RATIO": 24,
}


@dataclass(frozen=True)
class Seed:
    """一个装好种子数据的租户（含两个门店，便于验证跨门店边界）。"""

    database: Database
    tenant_id: uuid.UUID
    store_id: uuid.UUID
    other_store_id: uuid.UUID
    member_id: uuid.UUID
    product_id: uuid.UUID
    partner_id: uuid.UUID
    qty_base: Decimal = Decimal("192")
    version: int = 0

    @property
    def context(self) -> TenantContext:
        """会话用租户上下文（老板，主仓）。"""
        return TenantContext(
            tenant_id=self.tenant_id,
            store_id=self.store_id,
            member_id=self.member_id,
            role_code="boss",
            tenant_version=1,
        )

    def context_of(self, *, role_code: str, store_id: uuid.UUID | None = None) -> TenantContext:
        """换角色/门店的上下文（验证角色与跨门店越权用）。"""
        return TenantContext(
            tenant_id=self.tenant_id,
            store_id=store_id or self.store_id,
            member_id=self.member_id,
            role_code=role_code,
            tenant_version=1,
        )


def new_database() -> Database:
    """建一个内存库并建全部表。"""
    database = Database.from_url("sqlite+pysqlite:///:memory:")
    database.create_all()
    return database


def seed_shop(*, qty_base: Decimal = Decimal("192"), version: int = 0) -> Seed:
    """装种子：1 租户 + 2 门店 + 1 老板 + 1 商品(可乐/瓶) + 1 供应商 + 1 条库存 + 1 条换算。

    Args:
        qty_base: 主仓库存（基本单位「瓶」）。
        version: 库存行的 version（CAS 冲突测试用）。

    Returns:
        :class:`Seed`。
    """
    database = new_database()
    tenant_id = uuid.uuid4()
    store_id = uuid.uuid4()
    other_store_id = uuid.uuid4()
    member_id = uuid.uuid4()
    product_id = uuid.uuid4()
    partner_id = uuid.uuid4()

    with database.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=tenant_id, name="测试小店", plan_code="free", quota_monthly=300))
        session.add(Store(tenant_id=tenant_id, store_id=store_id, name="主仓", is_main=True))
        session.add(Store(tenant_id=tenant_id, store_id=other_store_id, name="二号店", is_main=False))
        session.add(
            Member(tenant_id=tenant_id, member_id=member_id, wx_openid=f"openid-{member_id}", name="老板")
        )
        session.add(
            Role(tenant_id=tenant_id, member_id=member_id, store_id=store_id, role_code="boss")
        )
        session.add(
            Product(
                tenant_id=tenant_id,
                product_id=product_id,
                name="可乐",
                spec="500ml",
                base_unit="瓶",
                last_purchase_price_fen=yuan_to_fen("35"),
                last_sale_price_fen=yuan_to_fen("40"),
            )
        )
        session.add(
            Partner(tenant_id=tenant_id, partner_id=partner_id, type="supplier", name="老张")
        )
        session.add(
            Inventory(
                tenant_id=tenant_id,
                store_id=store_id,
                product_id=product_id,
                qty_base=qty_base,
                version=version,
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
    return Seed(
        database=database,
        tenant_id=tenant_id,
        store_id=store_id,
        other_store_id=other_store_id,
        member_id=member_id,
        product_id=product_id,
        partner_id=partner_id,
        qty_base=qty_base,
        version=version,
    )


@pytest.fixture()
def seed() -> Iterable[Seed]:
    """pytest 夹具：装好种子数据的库（用完释放连接池）。"""
    seeded = seed_shop()
    try:
        yield seeded
    finally:
        clear()
        seeded.database.drop_all()
        seeded.database.dispose()


# ---------------------------------------------------------------------------
# 快照：只读证明的对比对象
# ---------------------------------------------------------------------------
def dump_snapshot(database: Database) -> dict[str, list[str]]:
    """把**所有表**的全量行拍成可比较的快照（只读证明的 A/B 面）。

    不比较 ``id()`` 或对象状态，而是比较「库里的实际内容」：
    每行压成 ``列=repr`` 的稳定字符串并按表排序，任何一次写入（哪怕只是
    ``version`` +1）都会让快照不等，测试因此不会「看起来没写其实写了」。
    """
    snapshot: dict[str, list[str]] = {}
    with database.session(bypass_scope=True) as session:
        for table in Base.metadata.sorted_tables:
            rows = session.execute(select(table)).mappings().all()
            snapshot[table.name] = sorted(_signature(row) for row in rows)
    return snapshot


def _signature(row: Mapping[str, Any]) -> str:
    """一行的稳定签名。"""
    return "|".join(f"{key}={row[key]!r}" for key in sorted(row))


# ---------------------------------------------------------------------------
# 槽位 / 解析结果 / 明细行 / 预演
# ---------------------------------------------------------------------------
def slots_for(intent_id: str) -> dict[str, Any]:
    """按契约 ``required_slots`` 生成一份最小可用的合法槽位（不硬编码意图→槽位）。"""
    spec = default_registry().require(intent_id)
    slots: dict[str, Any] = {}
    for code in spec.required_slots:
        slots[code] = _SLOT_SAMPLES.get(code, "测试值")
    return slots


def parse_of(
    intent_id: str,
    slots: Mapping[str, Any] | None = None,
    *,
    source: Mapping[str, str] | None = None,
    confidence: float = 0.95,
    calc_traces: Sequence[str] = (),
    channel: str = "template",
    route: str | None = None,
) -> ParseResult:
    """造一个已过 Schema 的 ``ParseResult``（``source`` 默认全 ``asr``）。"""
    resolved = dict(slots) if slots is not None else slots_for(intent_id)
    sources: dict[str, str] = {code: "asr" for code in resolved}
    for code, value in (source or {}).items():
        if code in resolved:  # source 的键必须与 slots 完全一致，多一个就非法
            sources[code] = value
    return ParseResult(
        intent=intent_id,
        slots=resolved,
        source=sources,
        confidence=confidence,
        calc_traces=list(calc_traces),
        channel=channel,
        route=route,
    )


def lines_for(
    seed: Seed,
    *,
    quantity: Any = 2,
    unit: str = "箱",
    unit_price_fen: int = 3500,
    qty_base: Any = 48,
    product_label: str = "可乐 500ml",
    partner_id: uuid.UUID | None = None,
    payment_status: str = "已结",
    pending: bool = False,
    with_product: bool = True,
) -> list[dict[str, Any]]:
    """造一行落账明细（``amount_fen = 单价 × 数量``，整数分）。"""
    qty = Decimal(str(quantity))
    amount_fen = int(unit_price_fen) * int(qty)
    return [
        {
            "product_id": str(seed.product_id) if with_product else "",
            "product_label": product_label,
            "quantity": quantity,
            "unit": unit,
            "qty_base": qty_base,
            "unit_price_fen": int(unit_price_fen),
            "amount_fen": amount_fen,
            "partner_id": str(partner_id or ""),
            "payment_status": payment_status,
            "calc_trace": f"{quantity}{unit} → {qty_base} 瓶（1{unit}=24瓶）",
            "pending": pending,
        }
    ]


def qty_base_of(seed: Seed):
    """折算函数：``箱 → 瓶``（读种子里的换算，24:1）。"""

    def _convert(_product_id: Any, quantity: Any, unit: str) -> Decimal:
        qty = Decimal(str(quantity or 0))
        return qty * 24 if unit == "箱" else qty

    del seed
    return _convert


def preview_for(
    seed: Seed,
    *,
    session: Any,
    intent_id: str = DEFAULT_INTENT,
    lines: Sequence[Mapping[str, Any]] | None = None,
    store_id: uuid.UUID | None = None,
) -> PreviewBundle:
    """跑一次真实只读预演（复用调用方会话，SQLite 上走 ``PRAGMA query_only`` 窗口）。"""
    spec = default_registry().require(intent_id)
    rows = list(lines if lines is not None else lines_for(seed))
    deltas = build_deltas(
        intent_id=intent_id,
        side_effect=dict(spec.side_effect or {}),
        lines=[dict(row) for row in rows if row.get("product_id")],
    )
    return ImpactPreviewService(seed.database).preview(
        tenant_id=seed.tenant_id,
        store_id=store_id or seed.store_id,
        deltas=deltas,
        session=session,
    )


def create_card_for(
    seed: Seed,
    *,
    session: Any,
    intent_id: str = DEFAULT_INTENT,
    slots: Mapping[str, Any] | None = None,
    source: Mapping[str, str] | None = None,
    lines: Sequence[Mapping[str, Any]] | None = None,
    preview: PreviewBundle | None = None,
    ttl: int = 30,
    now: datetime | None = None,
    session_id: uuid.UUID | None = None,
    asr_text: str = "进了两箱可乐，一件三十五",
    asr_confidence: float = 0.95,
    calc_traces: Sequence[str] = (),
) -> tuple[LedgerService, CardCreation]:
    """建一张确认卡片（预演 → 建卡 → 同事务扣 1 次），返回服务与结果。"""
    moment = now or datetime.now(timezone.utc)
    parsed = parse_of(
        intent_id, slots, source=source, calc_traces=calc_traces, confidence=asr_confidence
    )
    rows = list(lines if lines is not None else lines_for(seed))
    bundle = (
        preview
        if preview is not None
        else preview_for(seed, session=session, intent_id=intent_id, lines=rows)
    )
    ledger = LedgerService(session, card_ttl_seconds=ttl)
    creation = ledger.create_card(
        tenant_id=seed.tenant_id,
        store_id=seed.store_id,
        member_id=seed.member_id,
        session_id=session_id or uuid.uuid4(),
        parse=parsed,
        asr_text=asr_text,
        asr_confidence=asr_confidence,
        lines=rows,
        preview=bundle,
        now=moment,
    )
    return ledger, creation


def spec_of(intent_id: str) -> IntentSpec:
    """按 id 取意图规格（未登记直接抛）。"""
    return default_registry().require(intent_id)


def slot_codes() -> tuple[str, ...]:
    """契约的 18 个槽位代号。"""
    return tuple(get_contracts().slot_codes)


def count_rows(database: Database, model: Any) -> int:
    """数某张表的行数（用一个全新的绕过 scope 的会话，避免被租户过滤）。"""
    from sqlalchemy import func

    with database.session(bypass_scope=True) as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


__all__ += ["Seed", "spec_of", "slot_codes", "count_rows"]
