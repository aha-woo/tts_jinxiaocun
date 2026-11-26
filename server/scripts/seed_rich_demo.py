"""云端 PG / 本地库完整演示数据灌入脚本（方式 B-1，兼容 ``FORCE ROW LEVEL SECURITY``）。

为什么单独写这一份而不是只用 ``demo_seed.py``
---------------------------------------------
``demo_seed.py`` 只补了 2 件商品和换算，页面打开依然空荡荡。本脚本按「王姐便利店
批零结合」的真实一天，一次性灌入可完整体验所有页面的数据：

- 8 件商品（含分批/临期/低库存/缺进价/负库存预警）+ 单位换算（已批准）；
- 批次余量（可乐 2 批不同进价、雪碧 1 批、红牛 2 批含 3 天后临期）；
- 5 个往来单位（3 客户 + 2 供应商）；
- 今日与近几日共 11 笔单据（进货/卖货/赊账/部分结算/报损/销退/缺进价进货）；
- 应收应付（未结 + 部分结算 + 1 笔历史收款流水）；
- 统一待处理（缺进价 / 负库存 / 临期批次）+ 1 条大屏 R3 待办审批；
- 常用商品别名 & 个人说法词表。

多租户 / PG RLS 兼容性
----------------------
1. 只用 ``Tenant``（平台根表）查目标租户；
2. 查 ``store`` / ``member`` 以及全部读写均通过 ``database.session(TenantContext(...))``
   注入 ``app.tenant_id`` GUC 与 ORM scope，严格遵守 ``FORCE ROW LEVEL SECURITY``，
   无需超级用户或 ``BYPASSRLS``；
3. 所有主键均由 ``uuid5(DEMO_NAMESPACE, f"{tenant_id}:{slug}")`` 确定性派生，
   **可重复执行（幂等）**，也支持 ``--clean`` 只删演示数据、不动用户真实业务数据。

用法（云托管容器终端 WebShell 或本地）::

    python scripts/probe_env.py                  # 先确认 PG 与 RLS 正常
    python scripts/seed_rich_demo.py --list      # 查看库里的租户
    python scripts/seed_rich_demo.py             # 给最近登录的租户灌入完整演示数据
    python scripts/seed_rich_demo.py --tenant <uuid>
    python scripts/seed_rich_demo.py --clean     # 仅清理该租户的演示数据
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

#: 让 `python scripts/seed_rich_demo.py` 能直接 import 到 app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.models  # noqa: F401,E402 - 确保全部模型注册进 Base.metadata
from app.db.session import Database  # noqa: E402
from app.models.catalog import Product, ProductAlias, UnitConversion  # noqa: E402
from app.models.dict import PersonalDict  # noqa: E402
from app.models.doc import ArAp, DocHeader, DocLine, Payment  # noqa: E402
from app.models.inventory import Inventory, InventoryMovement, StockBatch  # noqa: E402
from app.models.ops import Gap, PendingTask  # noqa: E402
from app.models.org import Member, Role, Store  # noqa: E402
from app.models.partner import Partner  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.models.voice import VoiceSession  # noqa: E402
from app.tenant import rls  # noqa: E402
from app.tenant.context import TenantContext  # noqa: E402
from app.utils.date_utils import from_iso_date, today_in_tz  # noqa: E402

#: 演示数据确定性命名空间（绝不与正常 uuid4 碰撞）
DEMO_NAMESPACE = uuid.UUID("7f3b9a10-4b7f-4a12-9b81-202611260001")
NIL_UUID = uuid.UUID(int=0)


def _demo_uuid(tenant_id: uuid.UUID, slug: str) -> uuid.UUID:
    """按 (租户, 演示标识) 派生确定性 UUID，保证幂等与精确清理。"""
    return uuid.uuid5(DEMO_NAMESPACE, f"{tenant_id}:{slug}")


@dataclass(frozen=True)
class DemoSummary:
    """灌数或清理结果摘要。"""

    tenant_id: str
    tenant_name: str
    store_id: str
    member_id: str
    products: int = 0
    batches: int = 0
    partners: int = 0
    docs: int = 0
    ar_ap: int = 0
    gaps: int = 0
    aliases: int = 0
    tasks: int = 0


#: 商品规格：(slug, name, spec, base_unit, qty_base, purchase_fen, sale_fen, avg_cost_fen, track_batch, cost_method, low_stock_qty, expiry_warn_days, conversions)
_PRODUCTS: tuple[tuple[Any, ...], ...] = (
    ("prod-cola", "可乐", "500ml", "瓶", Decimal("240"), 150, 300, 145, True, "fifo", Decimal("24"), 7, (("箱", "瓶", "24"),)),
    ("prod-sprite", "雪碧", "500ml", "瓶", Decimal("120"), 142, 300, 142, True, "fifo", Decimal("24"), 7, (("箱", "瓶", "24"),)),
    ("prod-icetea", "冰红茶", "500ml", "瓶", Decimal("37"), 220, 400, 220, False, None, Decimal("15"), 7, (("箱", "瓶", "15"),)),
    ("prod-redbull", "红牛", "250ml", "罐", Decimal("4"), 450, 600, 460, True, "fefo", Decimal("6"), 7, (("箱", "罐", "24"),)),
    ("prod-water", "农夫山泉", "550ml", "瓶", Decimal("144"), 100, 200, 100, False, None, Decimal("24"), 7, (("箱", "瓶", "24"),)),
    ("prod-smoke", "软中华", "硬条", "条", Decimal("7"), 21000, 23000, 21000, False, None, Decimal("2"), 30, ()),
    ("prod-apple", "富士苹果", "散装", "斤", Decimal("10"), None, 500, None, False, None, Decimal("5"), 3, ()),
    ("prod-noodle", "康师傅红烧牛肉面", "桶装", "桶", Decimal("-3"), 320, 450, 320, False, None, Decimal("12"), 15, (("箱", "桶", "12"),)),
)

#: 往来单位：(slug, type, name, note)
_PARTNERS: tuple[tuple[str, str, str, str], ...] = (
    ("partner-li", "customer", "李婶", "老街坊，常赊烟酒"),
    ("partner-xingwang", "customer", "兴旺超市", "月结批发客户"),
    ("partner-erzhong", "customer", "二中门口小卖部", "周结水饮客户"),
    ("partner-zhao", "supplier", "南门老赵批发", "饮料/水主供货商"),
    ("partner-wang", "supplier", "王老板饮料行", "功能饮料与临时补货"),
)


def _database_url() -> str:
    """读库连接串。"""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "未设置 DATABASE_URL。示例：\n"
            '  Linux/容器：export DATABASE_URL="postgresql+psycopg://vil:***@172.17.0.13:5432/vil?sslmode=require"\n'
            '  本地SQLite：export DATABASE_URL="sqlite+pysqlite:///./dev.db"'
        )
    return url


def _load_tenant(database: Database, tenant_id: uuid.UUID) -> Tenant | None:
    """在绑定 ``tenant_id`` GUC 的会话内读取单个租户（PG 上 ``tenant`` 表同样受 FORCE RLS 约束）。"""
    ctx = TenantContext(
        tenant_id=tenant_id,
        store_id=NIL_UUID,
        member_id=NIL_UUID,
        role_code="boss",
        tenant_version=1,
    )
    with database.session(ctx) as session:
        return session.get(Tenant, tenant_id)


def _list_tenants(database: Database) -> list[Tenant]:
    """按 PG FORCE RLS 引导策略列出已登录租户（最近创建排前面）。

    ``tenant`` 表本身启用了 ``FORCE ROW LEVEL SECURITY``，无 ``app.tenant_id`` 时
    直查返回 0 行；故先借 ``member_login_lookup`` 引导策略从 ``member`` 取出
    ``tenant_id``，再逐个切入租户 GUC 读取 ``Tenant`` 记录。
    """
    with database.session(bypass_scope=True) as session:
        rls.set_auth_bootstrap(session, True)
        raw_tids = list(
            session.execute(select(Member.tenant_id).order_by(Member.created_at.desc())).scalars()
        )
        rls.set_auth_bootstrap(session, False)

    unique_tids = list(dict.fromkeys(raw_tids))
    tenants: list[Tenant] = []
    for tid in unique_tids:
        tenant = _load_tenant(database, tid)
        if tenant is not None:
            tenants.append(tenant)
    tenants.sort(key=lambda item: item.created_at, reverse=True)
    return tenants


def _resolve_store_and_member(
    database: Database,
    tenant: Tenant,
) -> tuple[uuid.UUID, uuid.UUID]:
    """在带 ``tenant_id`` GUC 的会话内解析主门店与老板/首个员工。"""
    probe_ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        store_id=NIL_UUID,
        member_id=NIL_UUID,
        role_code="boss",
        tenant_version=tenant.version,
    )
    with database.session(probe_ctx) as session:
        store = session.execute(
            select(Store)
            .where(Store.tenant_id == tenant.tenant_id)
            .order_by(Store.is_main.desc(), Store.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if store is None:
            raise RuntimeError(f"租户 {tenant.tenant_id} 下没有门店记录，请先登录小程序完成初始化")

        boss_role = session.execute(
            select(Role)
            .where(Role.tenant_id == tenant.tenant_id, Role.role_code == "boss")
            .limit(1)
        ).scalar_one_or_none()
        member: Member | None = None
        if boss_role is not None:
            member = session.execute(
                select(Member)
                .where(Member.tenant_id == tenant.tenant_id, Member.member_id == boss_role.member_id)
                .limit(1)
            ).scalar_one_or_none()
        if member is None:
            member = session.execute(
                select(Member)
                .where(Member.tenant_id == tenant.tenant_id)
                .order_by(Member.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
        if member is None:
            raise RuntimeError(f"租户 {tenant.tenant_id} 下没有员工记录，请先登录小程序完成初始化")
        return store.store_id, member.member_id


def _dt_on(biz_date: date, hour: int, minute: int) -> datetime:
    """构造业务日期当天指定时刻（UTC+8 偏移转 UTC 存储）。"""
    tz_cn = timezone(timedelta(hours=8))
    return datetime.combine(biz_date, time(hour=hour, minute=minute), tzinfo=tz_cn).astimezone(timezone.utc)


def _upsert_product(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    row: tuple[Any, ...],
) -> Product:
    """幂等写入或更新商品、换算与门店库存。"""
    (
        slug,
        name,
        spec,
        base_unit,
        qty_base,
        purchase_fen,
        sale_fen,
        avg_cost_fen,
        track_batch,
        cost_method,
        low_stock_qty,
        expiry_warn_days,
        conversions,
    ) = row
    demo_pid = _demo_uuid(tenant_id, slug)

    # 若租户此前自己建过同名同规格商品（如 demo_seed 建的可乐 500ml），优先复用其 ID
    existing = session.execute(
        select(Product)
        .where(
            Product.tenant_id == tenant_id,
            (Product.product_id == demo_pid) | ((Product.name == name) & (Product.spec == spec)),
        )
        .limit(1)
    ).scalar_one_or_none()

    if existing is None:
        product = Product(
            tenant_id=tenant_id,
            product_id=demo_pid,
            name=name,
            spec=spec,
            base_unit=base_unit,
            last_purchase_price_fen=purchase_fen,
            last_sale_price_fen=sale_fen,
            avg_cost_fen=avg_cost_fen,
            track_batch=track_batch,
            cost_method=cost_method,
            low_stock_qty=low_stock_qty,
            expiry_warn_days=expiry_warn_days,
            status="active",
        )
        session.add(product)
    else:
        product = existing
        product.base_unit = base_unit
        product.last_purchase_price_fen = purchase_fen
        product.last_sale_price_fen = sale_fen
        product.avg_cost_fen = avg_cost_fen
        product.track_batch = track_batch
        product.cost_method = cost_method
        product.low_stock_qty = low_stock_qty
        product.expiry_warn_days = expiry_warn_days
        product.status = "active"
    session.flush()

    inv = session.execute(
        select(Inventory)
        .where(
            Inventory.tenant_id == tenant_id,
            Inventory.store_id == store_id,
            Inventory.product_id == product.product_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if inv is None:
        session.add(
            Inventory(
                tenant_id=tenant_id,
                store_id=store_id,
                product_id=product.product_id,
                qty_base=qty_base,
                avg_cost_fen=avg_cost_fen,
                negative_flag=qty_base < 0,
                version=0,
            )
        )
    else:
        inv.qty_base = qty_base
        inv.avg_cost_fen = avg_cost_fen
        inv.negative_flag = qty_base < 0

    for from_u, to_u, ratio_str in conversions:
        conv = session.execute(
            select(UnitConversion)
            .where(
                UnitConversion.tenant_id == tenant_id,
                UnitConversion.product_id == product.product_id,
                UnitConversion.from_unit == from_u,
                UnitConversion.to_unit == to_u,
            )
            .limit(1)
        ).scalar_one_or_none()
        if conv is None:
            session.add(
                UnitConversion(
                    conv_id=_demo_uuid(tenant_id, f"conv-{slug}-{from_u}-{to_u}"),
                    tenant_id=tenant_id,
                    product_id=product.product_id,
                    from_unit=from_u,
                    to_unit=to_u,
                    ratio=Decimal(ratio_str),
                    status="approved",
                )
            )
        else:
            conv.ratio = Decimal(ratio_str)
            conv.status = "approved"

    session.flush()
    return product


def _upsert_partner(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    ptype: str,
    name: str,
    note: str,
) -> Partner:
    """幂等写入往来单位。"""
    demo_id = _demo_uuid(tenant_id, slug)
    partner = session.execute(
        select(Partner)
        .where(
            Partner.tenant_id == tenant_id,
            (Partner.partner_id == demo_id) | ((Partner.type == ptype) & (Partner.name == name)),
        )
        .limit(1)
    ).scalar_one_or_none()
    if partner is None:
        partner = Partner(
            partner_id=demo_id,
            tenant_id=tenant_id,
            type=ptype,
            name=name,
            note=note,
            status="active",
        )
        session.add(partner)
    else:
        partner.note = note
        partner.status = "active"
    session.flush()
    return partner


def _upsert_batch(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    slug: str,
    product_id: uuid.UUID,
    partner_id: uuid.UUID | None,
    label: str,
    qty_in: Decimal,
    qty_left: Decimal,
    unit_cost_fen: int | None,
    expiry_date: date | None,
    received_at: datetime,
) -> StockBatch:
    """幂等写入入库批次。"""
    batch_id = _demo_uuid(tenant_id, slug)
    batch = session.execute(
        select(StockBatch)
        .where(StockBatch.tenant_id == tenant_id, StockBatch.batch_id == batch_id)
        .limit(1)
    ).scalar_one_or_none()
    status = "exhausted" if qty_left == 0 else "open"
    if batch is None:
        batch = StockBatch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            store_id=store_id,
            product_id=product_id,
            partner_id=partner_id,
            label=label,
            is_virtual=False,
            qty_in=qty_in,
            qty_left=qty_left,
            unit_cost_fen=unit_cost_fen,
            expiry_date=expiry_date,
            received_at=received_at,
            status=status,
        )
        session.add(batch)
    else:
        batch.label = label
        batch.qty_in = qty_in
        batch.qty_left = qty_left
        batch.unit_cost_fen = unit_cost_fen
        batch.expiry_date = expiry_date
        batch.received_at = received_at
        batch.status = status
    session.flush()
    return batch


def _upsert_doc(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    slug: str,
    doc_no: str,
    intent_id: str,
    doc_type: str,
    partner_id: uuid.UUID | None,
    biz_date: date,
    created_at: datetime,
    payment_status: str,
    total_amount_fen: int,
    cogs_fen: int | None,
    source: str,
    lines: list[dict[str, Any]],
) -> DocHeader:
    """幂等写入单据头、明细行及对应库存流水。"""
    doc_id = _demo_uuid(tenant_id, slug)
    header = session.execute(
        select(DocHeader)
        .where(
            DocHeader.tenant_id == tenant_id,
            (DocHeader.doc_id == doc_id) | (DocHeader.doc_no == doc_no),
        )
        .limit(1)
    ).scalar_one_or_none()
    if header is None:
        header = DocHeader(
            doc_id=doc_id,
            tenant_id=tenant_id,
            store_id=store_id,
            doc_no=doc_no,
            intent_id=intent_id,
            doc_type=doc_type,
            partner_id=partner_id,
            biz_date=biz_date,
            payment_status=payment_status,
            total_amount_fen=total_amount_fen,
            cogs_fen=cogs_fen,
            status="confirmed",
            source=source,
            created_by=member_id,
            created_at=created_at,
        )
        session.add(header)
    else:
        header.intent_id = intent_id
        header.doc_type = doc_type
        header.partner_id = partner_id
        header.biz_date = biz_date
        header.payment_status = payment_status
        header.total_amount_fen = total_amount_fen
        header.cogs_fen = cogs_fen
        header.status = "confirmed"
        header.source = source
        header.created_at = created_at
    session.flush()

    for idx, line_spec in enumerate(lines, start=1):
        line_id = _demo_uuid(tenant_id, f"{slug}-line-{idx}")
        line = session.execute(
            select(DocLine)
            .where(DocLine.tenant_id == tenant_id, DocLine.line_id == line_id)
            .limit(1)
        ).scalar_one_or_none()
        if line is None:
            line = DocLine(
                line_id=line_id,
                tenant_id=tenant_id,
                store_id=store_id,
                doc_id=header.doc_id,
                product_id=line_spec["product_id"],
                batch_id=line_spec.get("batch_id"),
                qty=line_spec["qty"],
                unit=line_spec["unit"],
                qty_base=line_spec["qty_base"],
                unit_price_fen=line_spec.get("unit_price_fen"),
                amount_fen=line_spec.get("amount_fen"),
                price_source=line_spec.get("price_source"),
                cost_snapshot_fen=line_spec.get("cost_snapshot_fen"),
                cost_status=line_spec.get("cost_status", "final"),
                created_at=created_at,
            )
            session.add(line)
        else:
            line.product_id = line_spec["product_id"]
            line.batch_id = line_spec.get("batch_id")
            line.qty = line_spec["qty"]
            line.unit = line_spec["unit"]
            line.qty_base = line_spec["qty_base"]
            line.unit_price_fen = line_spec.get("unit_price_fen")
            line.amount_fen = line_spec.get("amount_fen")
            line.price_source = line_spec.get("price_source")
            line.cost_snapshot_fen = line_spec.get("cost_snapshot_fen")
            line.cost_status = line_spec.get("cost_status", "final")

        move_delta = line_spec["move_qty_base"]
        if move_delta != 0:
            move_id = _demo_uuid(tenant_id, f"{slug}-move-{idx}")
            move = session.execute(
                select(InventoryMovement)
                .where(InventoryMovement.tenant_id == tenant_id, InventoryMovement.move_id == move_id)
                .limit(1)
            ).scalar_one_or_none()
            if move is None:
                session.add(
                    InventoryMovement(
                        move_id=move_id,
                        tenant_id=tenant_id,
                        store_id=store_id,
                        product_id=line_spec["product_id"],
                        batch_id=line_spec.get("batch_id"),
                        doc_id=header.doc_id,
                        doc_line_id=line_id,
                        reason=line_spec["move_reason"],
                        qty_base=move_delta,
                        unit_cost_fen=line_spec.get("unit_cost_fen"),
                        cost_status=line_spec.get("cost_status", "final"),
                        partner_id=partner_id,
                        occurred_at=created_at,
                        created_by=member_id,
                    )
                )
    session.flush()
    return header


def _upsert_ar_ap(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    slug: str,
    partner_id: uuid.UUID,
    doc_id: uuid.UUID,
    direction: str,
    amount_fen: int,
    settled_amount_fen: int,
    status: str,
    created_at: datetime,
) -> ArAp:
    """幂等写入应收/应付记录。"""
    ar_ap_id = _demo_uuid(tenant_id, slug)
    row = session.execute(
        select(ArAp)
        .where(
            ArAp.tenant_id == tenant_id,
            (ArAp.ar_ap_id == ar_ap_id) | ((ArAp.doc_id == doc_id) & (ArAp.direction == direction)),
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = ArAp(
            ar_ap_id=ar_ap_id,
            tenant_id=tenant_id,
            store_id=store_id,
            partner_id=partner_id,
            doc_id=doc_id,
            direction=direction,
            amount_fen=amount_fen,
            settled_amount_fen=settled_amount_fen,
            status=status,
            version=0,
            created_at=created_at,
        )
        session.add(row)
    else:
        row.amount_fen = amount_fen
        row.settled_amount_fen = settled_amount_fen
        row.status = status
    session.flush()
    return row


def seed_tenant(database: Database, tenant: Tenant) -> DemoSummary:
    """为指定租户幂等灌入完整演示数据（全程走租户上下文 + PG RLS）。"""
    store_id, member_id = _resolve_store_and_member(database, tenant)
    ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        store_id=store_id,
        member_id=member_id,
        role_code="boss",
        tenant_version=tenant.version,
    )
    today = from_iso_date(today_in_tz())
    d_1 = today - timedelta(days=1)
    d_2 = today - timedelta(days=2)
    d_3 = today - timedelta(days=3)
    d_4 = today - timedelta(days=4)

    with database.session(ctx) as session:
        # 1. 商品 + 库存 + 换算
        products: dict[str, Product] = {}
        for row in _PRODUCTS:
            slug = row[0]
            products[slug] = _upsert_product(
                session, tenant_id=tenant.tenant_id, store_id=store_id, row=row
            )

        # 2. 往来单位
        partners: dict[str, Partner] = {}
        for slug, ptype, name, note in _PARTNERS:
            partners[slug] = _upsert_partner(
                session, tenant_id=tenant.tenant_id, slug=slug, ptype=ptype, name=name, note=note
            )

        # 3. 批次（含临期与多成本）
        b_cola_1 = _upsert_batch(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="batch-cola-1",
            product_id=products["prod-cola"].product_id,
            partner_id=partners["partner-zhao"].partner_id,
            label=f"{d_4:%m/%d} 南门老赵",
            qty_in=Decimal("192"),
            qty_left=Decimal("192"),
            unit_cost_fen=142,
            expiry_date=None,
            received_at=_dt_on(d_4, 9, 10),
        )
        b_cola_2 = _upsert_batch(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="batch-cola-2",
            product_id=products["prod-cola"].product_id,
            partner_id=partners["partner-wang"].partner_id,
            label=f"{today:%m/%d} 王老板",
            qty_in=Decimal("48"),
            qty_left=Decimal("48"),
            unit_cost_fen=150,
            expiry_date=None,
            received_at=_dt_on(today, 7, 42),
        )
        _upsert_batch(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="batch-sprite-1",
            product_id=products["prod-sprite"].product_id,
            partner_id=partners["partner-zhao"].partner_id,
            label=f"{d_3:%m/%d} 南门老赵",
            qty_in=Decimal("120"),
            qty_left=Decimal("120"),
            unit_cost_fen=142,
            expiry_date=None,
            received_at=_dt_on(d_3, 10, 0),
        )
        b_redbull_exp = _upsert_batch(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="batch-redbull-exp",
            product_id=products["prod-redbull"].product_id,
            partner_id=partners["partner-wang"].partner_id,
            label=f"{d_4:%m/%d} 临期批",
            qty_in=Decimal("24"),
            qty_left=Decimal("2"),
            unit_cost_fen=450,
            expiry_date=today + timedelta(days=3),
            received_at=_dt_on(d_4, 11, 20),
        )
        _upsert_batch(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="batch-redbull-new",
            product_id=products["prod-redbull"].product_id,
            partner_id=partners["partner-wang"].partner_id,
            label=f"{d_1:%m/%d} 新批",
            qty_in=Decimal("24"),
            qty_left=Decimal("2"),
            unit_cost_fen=480,
            expiry_date=today + timedelta(days=90),
            received_at=_dt_on(d_1, 14, 15),
        )

        # 4. 单据（覆盖今日流水 + 本周经营 + 缺进价 + 赊账 + 报损 + 退货）
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-cg-001",
            doc_no="DEMO-CG-001",
            intent_id="purchase.cash",
            doc_type="purchase",
            partner_id=partners["partner-wang"].partner_id,
            biz_date=today,
            created_at=_dt_on(today, 7, 42),
            payment_status="已结",
            total_amount_fen=7200,
            cogs_fen=None,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-cola"].product_id,
                    "batch_id": b_cola_2.batch_id,
                    "qty": Decimal("2"),
                    "unit": "箱",
                    "qty_base": Decimal("48"),
                    "unit_price_fen": 3600,
                    "amount_fen": 7200,
                    "price_source": "asr",
                    "cost_snapshot_fen": 7200,
                    "cost_status": "final",
                    "move_qty_base": Decimal("48"),
                    "move_reason": "purchase",
                    "unit_cost_fen": 150,
                }
            ],
        )
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-cg-002",
            doc_no="DEMO-CG-002",
            intent_id="purchase.cash",
            doc_type="purchase",
            partner_id=partners["partner-zhao"].partner_id,
            biz_date=today,
            created_at=_dt_on(today, 8, 15),
            payment_status="已结",
            total_amount_fen=0,
            cogs_fen=None,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-apple"].product_id,
                    "qty": Decimal("10"),
                    "unit": "斤",
                    "qty_base": Decimal("10"),
                    "unit_price_fen": None,
                    "amount_fen": None,
                    "price_source": "pending",
                    "cost_snapshot_fen": None,
                    "cost_status": "unknown",
                    "move_qty_base": Decimal("10"),
                    "move_reason": "purchase",
                    "unit_cost_fen": None,
                }
            ],
        )
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xs-001",
            doc_no="DEMO-XS-001",
            intent_id="sales.cash",
            doc_type="sales",
            partner_id=None,
            biz_date=today,
            created_at=_dt_on(today, 9, 10),
            payment_status="已结",
            total_amount_fen=4800,
            cogs_fen=2520,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-icetea"].product_id,
                    "qty": Decimal("6"),
                    "unit": "瓶",
                    "qty_base": Decimal("6"),
                    "unit_price_fen": 400,
                    "amount_fen": 2400,
                    "price_source": "asr",
                    "cost_snapshot_fen": 1320,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-6"),
                    "move_reason": "sale",
                    "unit_cost_fen": 220,
                },
                {
                    "product_id": products["prod-water"].product_id,
                    "qty": Decimal("12"),
                    "unit": "瓶",
                    "qty_base": Decimal("12"),
                    "unit_price_fen": 200,
                    "amount_fen": 2400,
                    "price_source": "asr",
                    "cost_snapshot_fen": 1200,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-12"),
                    "move_reason": "sale",
                    "unit_cost_fen": 100,
                },
            ],
        )
        doc_xs_li = _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xs-002",
            doc_no="DEMO-XS-002",
            intent_id="sales.credit",
            doc_type="sales",
            partner_id=partners["partner-li"].partner_id,
            biz_date=today,
            created_at=_dt_on(today, 9, 45),
            payment_status="未结",
            total_amount_fen=23000,
            cogs_fen=21000,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-smoke"].product_id,
                    "qty": Decimal("1"),
                    "unit": "条",
                    "qty_base": Decimal("1"),
                    "unit_price_fen": 23000,
                    "amount_fen": 23000,
                    "price_source": "asr",
                    "cost_snapshot_fen": 21000,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-1"),
                    "move_reason": "sale",
                    "unit_cost_fen": 21000,
                }
            ],
        )
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xs-003",
            doc_no="DEMO-XS-003",
            intent_id="sales.cash",
            doc_type="sales",
            partner_id=None,
            biz_date=today,
            created_at=_dt_on(today, 10, 20),
            payment_status="已结",
            total_amount_fen=1200,
            cogs_fen=900,
            source="manual",
            lines=[
                {
                    "product_id": products["prod-redbull"].product_id,
                    "batch_id": b_redbull_exp.batch_id,
                    "qty": Decimal("2"),
                    "unit": "罐",
                    "qty_base": Decimal("2"),
                    "unit_price_fen": 600,
                    "amount_fen": 1200,
                    "price_source": "default",
                    "cost_snapshot_fen": 900,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-2"),
                    "move_reason": "sale",
                    "unit_cost_fen": 450,
                }
            ],
        )
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-bs-001",
            doc_no="DEMO-BS-001",
            intent_id="inventory.loss",
            doc_type="waste",
            partner_id=None,
            biz_date=today,
            created_at=_dt_on(today, 11, 5),
            payment_status="已结",
            total_amount_fen=440,
            cogs_fen=440,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-icetea"].product_id,
                    "qty": Decimal("2"),
                    "unit": "瓶",
                    "qty_base": Decimal("2"),
                    "unit_price_fen": 220,
                    "amount_fen": 440,
                    "price_source": "computed",
                    "cost_snapshot_fen": 440,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-2"),
                    "move_reason": "loss",
                    "unit_cost_fen": 220,
                }
            ],
        )
        doc_xs_xw = _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xs-004",
            doc_no="DEMO-XS-004",
            intent_id="sales.credit",
            doc_type="sales",
            partner_id=partners["partner-xingwang"].partner_id,
            biz_date=d_3,
            created_at=_dt_on(d_3, 15, 20),
            payment_status="未结",
            total_amount_fen=120000,
            cogs_fen=68160,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-cola"].product_id,
                    "batch_id": b_cola_1.batch_id,
                    "qty": Decimal("20"),
                    "unit": "箱",
                    "qty_base": Decimal("480"),
                    "unit_price_fen": 6000,
                    "amount_fen": 120000,
                    "price_source": "asr",
                    "cost_snapshot_fen": 68160,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-480"),
                    "move_reason": "sale",
                    "unit_cost_fen": 142,
                }
            ],
        )
        doc_xs_ez = _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xs-005",
            doc_no="DEMO-XS-005",
            intent_id="sales.credit",
            doc_type="sales",
            partner_id=partners["partner-erzhong"].partner_id,
            biz_date=d_2,
            created_at=_dt_on(d_2, 16, 40),
            payment_status="部分",
            total_amount_fen=72000,
            cogs_fen=36000,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-water"].product_id,
                    "qty": Decimal("15"),
                    "unit": "箱",
                    "qty_base": Decimal("360"),
                    "unit_price_fen": 4800,
                    "amount_fen": 72000,
                    "price_source": "asr",
                    "cost_snapshot_fen": 36000,
                    "cost_status": "final",
                    "move_qty_base": Decimal("-360"),
                    "move_reason": "sale",
                    "unit_cost_fen": 100,
                }
            ],
        )
        doc_cg_zhao = _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-cg-003",
            doc_no="DEMO-CG-003",
            intent_id="purchase.credit",
            doc_type="purchase",
            partner_id=partners["partner-zhao"].partner_id,
            biz_date=d_4,
            created_at=_dt_on(d_4, 9, 10),
            payment_status="未结",
            total_amount_fen=72000,
            cogs_fen=None,
            source="photo",
            lines=[
                {
                    "product_id": products["prod-water"].product_id,
                    "qty": Decimal("30"),
                    "unit": "箱",
                    "qty_base": Decimal("720"),
                    "unit_price_fen": 2400,
                    "amount_fen": 72000,
                    "price_source": "asr",
                    "cost_snapshot_fen": 72000,
                    "cost_status": "final",
                    "move_qty_base": Decimal("720"),
                    "move_reason": "purchase",
                    "unit_cost_fen": 100,
                }
            ],
        )
        doc_cg_wang = _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-cg-004",
            doc_no="DEMO-CG-004",
            intent_id="purchase.credit",
            doc_type="purchase",
            partner_id=partners["partner-wang"].partner_id,
            biz_date=d_1,
            created_at=_dt_on(d_1, 14, 15),
            payment_status="未结",
            total_amount_fen=43200,
            cogs_fen=None,
            source="photo",
            lines=[
                {
                    "product_id": products["prod-redbull"].product_id,
                    "qty": Decimal("4"),
                    "unit": "箱",
                    "qty_base": Decimal("96"),
                    "unit_price_fen": 10800,
                    "amount_fen": 43200,
                    "price_source": "asr",
                    "cost_snapshot_fen": 43200,
                    "cost_status": "final",
                    "move_qty_base": Decimal("96"),
                    "move_reason": "purchase",
                    "unit_cost_fen": 450,
                }
            ],
        )
        _upsert_doc(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            member_id=member_id,
            slug="doc-xt-001",
            doc_no="DEMO-XT-001",
            intent_id="sales.return",
            doc_type="sales_return",
            partner_id=partners["partner-li"].partner_id,
            biz_date=d_1,
            created_at=_dt_on(d_1, 17, 30),
            payment_status="已结",
            total_amount_fen=600,
            cogs_fen=450,
            source="voice",
            lines=[
                {
                    "product_id": products["prod-redbull"].product_id,
                    "qty": Decimal("1"),
                    "unit": "罐",
                    "qty_base": Decimal("1"),
                    "unit_price_fen": 600,
                    "amount_fen": 600,
                    "price_source": "asr",
                    "cost_snapshot_fen": 450,
                    "cost_status": "final",
                    "move_qty_base": Decimal("1"),
                    "move_reason": "sales_return",
                    "unit_cost_fen": 450,
                }
            ],
        )

        # 5. 应收应付 + 历史收款流水
        _upsert_ar_ap(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="ar-li",
            partner_id=partners["partner-li"].partner_id,
            doc_id=doc_xs_li.doc_id,
            direction="receivable",
            amount_fen=23000,
            settled_amount_fen=0,
            status="open",
            created_at=_dt_on(today, 9, 45),
        )
        _upsert_ar_ap(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="ar-xingwang",
            partner_id=partners["partner-xingwang"].partner_id,
            doc_id=doc_xs_xw.doc_id,
            direction="receivable",
            amount_fen=120000,
            settled_amount_fen=0,
            status="open",
            created_at=_dt_on(d_3, 15, 20),
        )
        ar_erzhong = _upsert_ar_ap(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="ar-erzhong",
            partner_id=partners["partner-erzhong"].partner_id,
            doc_id=doc_xs_ez.doc_id,
            direction="receivable",
            amount_fen=72000,
            settled_amount_fen=26000,
            status="partial",
            created_at=_dt_on(d_2, 16, 40),
        )
        pay_id = _demo_uuid(tenant.tenant_id, "pay-erzhong-1")
        if (
            session.execute(
                select(Payment).where(Payment.tenant_id == tenant.tenant_id, Payment.payment_id == pay_id).limit(1)
            ).scalar_one_or_none()
            is None
        ):
            session.add(
                Payment(
                    payment_id=pay_id,
                    tenant_id=tenant.tenant_id,
                    store_id=store_id,
                    ar_ap_id=ar_erzhong.ar_ap_id,
                    amount_fen=26000,
                    method="微信",
                    biz_date=d_1,
                    idempotency_key=f"demo-pay-{tenant.tenant_id}-erzhong-1",
                    note="演示部分还款",
                )
            )
        _upsert_ar_ap(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="ap-zhao",
            partner_id=partners["partner-zhao"].partner_id,
            doc_id=doc_cg_zhao.doc_id,
            direction="payable",
            amount_fen=72000,
            settled_amount_fen=0,
            status="open",
            created_at=_dt_on(d_4, 9, 10),
        )
        _upsert_ar_ap(
            session,
            tenant_id=tenant.tenant_id,
            store_id=store_id,
            slug="ap-wang",
            partner_id=partners["partner-wang"].partner_id,
            doc_id=doc_cg_wang.doc_id,
            direction="payable",
            amount_fen=43200,
            settled_amount_fen=0,
            status="open",
            created_at=_dt_on(d_1, 14, 15),
        )

        # 6. 待处理（缺进价 / 负库存 / 临期批次）
        gap_specs = (
            (
                "gap-missing-cost-apple",
                "missing_cost",
                "product",
                products["prod-apple"].product_id,
                "富士苹果 · 进货 10 斤缺进价",
                {"product_id": str(products["prod-apple"].product_id), "unit": "斤", "qty": "10"},
                80,
            ),
            (
                "gap-negative-noodle",
                "negative_stock",
                "product",
                products["prod-noodle"].product_id,
                "康师傅红烧牛肉面 · 当前 -3 桶（负库存）",
                {"product_id": str(products["prod-noodle"].product_id), "qty_base": "-3"},
                90,
            ),
            (
                "gap-expiring-redbull",
                "expiring_batch",
                "stock_batch",
                b_redbull_exp.batch_id,
                "红牛 · 还有 2 罐 3 天后到期",
                {"batch_id": str(b_redbull_exp.batch_id), "expiry_date": str(today + timedelta(days=3))},
                70,
            ),
        )
        for slug, kind, ref_entity, ref_id, title, detail, priority in gap_specs:
            gid = _demo_uuid(tenant.tenant_id, slug)
            gap = session.execute(
                select(Gap).where(Gap.tenant_id == tenant.tenant_id, Gap.gap_id == gid).limit(1)
            ).scalar_one_or_none()
            if gap is None:
                session.add(
                    Gap(
                        gap_id=gid,
                        tenant_id=tenant.tenant_id,
                        store_id=store_id,
                        kind=kind,
                        ref_entity=ref_entity,
                        ref_id=ref_id,
                        title=title,
                        detail_json=detail,
                        priority=priority,
                        status="open",
                    )
                )

        # 7. 别名 & 个人词表
        alias_specs = (
            ("alias-cola", "快乐水", products["prod-cola"]),
            ("alias-water", "大水", products["prod-water"]),
            ("alias-redbull", "红罐", products["prod-redbull"]),
            ("alias-smoke", "烟", products["prod-smoke"]),
        )
        for slug, alias_text, product in alias_specs:
            aid = _demo_uuid(tenant.tenant_id, slug)
            alias = session.execute(
                select(ProductAlias)
                .where(
                    ProductAlias.tenant_id == tenant.tenant_id,
                    (ProductAlias.alias_id == aid)
                    | ((ProductAlias.member_id.is_(None)) & (ProductAlias.alias_text == alias_text)),
                )
                .limit(1)
            ).scalar_one_or_none()
            if alias is None:
                session.add(
                    ProductAlias(
                        alias_id=aid,
                        tenant_id=tenant.tenant_id,
                        product_id=product.product_id,
                        member_id=None,
                        alias_text=alias_text,
                        source="personal",
                    )
                )
            did = _demo_uuid(tenant.tenant_id, f"dict-{slug}")
            pdict = session.execute(
                select(PersonalDict)
                .where(
                    PersonalDict.tenant_id == tenant.tenant_id,
                    (PersonalDict.dict_id == did)
                    | (
                        (PersonalDict.member_id == member_id)
                        & (PersonalDict.raw_text == alias_text)
                        & (PersonalDict.mapped_type == "product")
                    ),
                )
                .limit(1)
            ).scalar_one_or_none()
            if pdict is None:
                session.add(
                    PersonalDict(
                        dict_id=did,
                        tenant_id=tenant.tenant_id,
                        member_id=member_id,
                        layer="personal",
                        raw_text=alias_text,
                        mapped_type="product",
                        mapped_id=product.product_id,
                        version=1,
                    )
                )

        # 8. R3 待办（电脑版微信大屏「R3 待办审批」用）
        task_session_id = _demo_uuid(tenant.tenant_id, "task-session-merge")
        if (
            session.execute(
                select(VoiceSession)
                .where(
                    VoiceSession.tenant_id == tenant.tenant_id,
                    VoiceSession.session_id == task_session_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            is None
        ):
            session.add(
                VoiceSession(
                    session_id=task_session_id,
                    tenant_id=tenant.tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    state="CLOSED_WRITE",
                    clarify_round=0,
                )
            )
        task_id = _demo_uuid(tenant.tenant_id, "task-partner-merge")
        if (
            session.execute(
                select(PendingTask)
                .where(PendingTask.tenant_id == tenant.tenant_id, PendingTask.task_id == task_id)
                .limit(1)
            ).scalar_one_or_none()
            is None
        ):
            session.add(
                PendingTask(
                    task_id=task_id,
                    tenant_id=tenant.tenant_id,
                    session_id=task_session_id,
                    intent_id="partner.merge",
                    payload={
                        "source_name": "老赵",
                        "target_name": "南门老赵批发",
                        "target_partner_id": str(partners["partner-zhao"].partner_id),
                        "note": "演示待办：高风险合并需在电脑版微信大屏由老板二次确认",
                    },
                    status="pending",
                )
            )

    return DemoSummary(
        tenant_id=str(tenant.tenant_id),
        tenant_name=str(tenant.name),
        store_id=str(store_id),
        member_id=str(member_id),
        products=len(_PRODUCTS),
        batches=5,
        partners=len(_PARTNERS),
        docs=11,
        ar_ap=5,
        gaps=len(gap_specs),
        aliases=len(alias_specs),
        tasks=1,
    )


def clean_tenant(database: Database, tenant: Tenant) -> DemoSummary:
    """仅删除该租户下由本脚本生成的演示数据（不动真实业务单据与档案）。"""
    store_id, member_id = _resolve_store_and_member(database, tenant)
    ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        store_id=store_id,
        member_id=member_id,
        role_code="boss",
        tenant_version=tenant.version,
    )
    tid = tenant.tenant_id

    doc_slugs = [
        "doc-cg-001", "doc-cg-002", "doc-cg-003", "doc-cg-004",
        "doc-xs-001", "doc-xs-002", "doc-xs-003", "doc-xs-004", "doc-xs-005",
        "doc-bs-001", "doc-xt-001",
    ]
    doc_ids = [_demo_uuid(tid, s) for s in doc_slugs]
    line_ids = [_demo_uuid(tid, f"{s}-line-{i}") for s in doc_slugs for i in (1, 2)]
    move_ids = [_demo_uuid(tid, f"{s}-move-{i}") for s in doc_slugs for i in (1, 2)]
    batch_ids = [
        _demo_uuid(tid, s)
        for s in ("batch-cola-1", "batch-cola-2", "batch-sprite-1", "batch-redbull-exp", "batch-redbull-new")
    ]
    ar_ap_ids = [_demo_uuid(tid, s) for s in ("ar-li", "ar-xingwang", "ar-erzhong", "ap-zhao", "ap-wang")]
    pay_ids = [_demo_uuid(tid, "pay-erzhong-1")]
    gap_ids = [
        _demo_uuid(tid, s)
        for s in ("gap-missing-cost-apple", "gap-negative-noodle", "gap-expiring-redbull")
    ]
    alias_slugs = ("alias-cola", "alias-water", "alias-redbull", "alias-smoke")
    alias_ids = [_demo_uuid(tid, s) for s in alias_slugs]
    dict_ids = [_demo_uuid(tid, f"dict-{s}") for s in alias_slugs]
    task_ids = [_demo_uuid(tid, "task-partner-merge")]
    task_session_ids = [_demo_uuid(tid, "task-session-merge")]
    product_ids = [_demo_uuid(tid, row[0]) for row in _PRODUCTS]
    conv_ids = [
        _demo_uuid(tid, f"conv-{row[0]}-{c[0]}-{c[1]}")
        for row in _PRODUCTS
        for c in row[12]
    ]
    partner_ids = [_demo_uuid(tid, row[0]) for row in _PARTNERS]

    with database.session(ctx) as session:
        session.execute(delete(Payment).where(Payment.tenant_id == tid, Payment.payment_id.in_(pay_ids)))
        session.execute(delete(ArAp).where(ArAp.tenant_id == tid, ArAp.ar_ap_id.in_(ar_ap_ids)))
        session.execute(delete(Gap).where(Gap.tenant_id == tid, Gap.gap_id.in_(gap_ids)))
        session.execute(delete(PendingTask).where(PendingTask.tenant_id == tid, PendingTask.task_id.in_(task_ids)))
        session.execute(delete(VoiceSession).where(VoiceSession.tenant_id == tid, VoiceSession.session_id.in_(task_session_ids)))
        session.execute(delete(InventoryMovement).where(InventoryMovement.tenant_id == tid, InventoryMovement.move_id.in_(move_ids)))
        session.execute(delete(StockBatch).where(StockBatch.tenant_id == tid, StockBatch.batch_id.in_(batch_ids)))
        session.execute(delete(DocLine).where(DocLine.tenant_id == tid, DocLine.line_id.in_(line_ids)))
        session.execute(delete(DocHeader).where(DocHeader.tenant_id == tid, DocHeader.doc_id.in_(doc_ids)))
        session.execute(delete(PersonalDict).where(PersonalDict.tenant_id == tid, PersonalDict.dict_id.in_(dict_ids)))
        session.execute(delete(ProductAlias).where(ProductAlias.tenant_id == tid, ProductAlias.alias_id.in_(alias_ids)))
        session.execute(delete(UnitConversion).where(UnitConversion.tenant_id == tid, UnitConversion.conv_id.in_(conv_ids)))
        session.execute(delete(Inventory).where(Inventory.tenant_id == tid, Inventory.product_id.in_(product_ids)))
        session.execute(delete(Product).where(Product.tenant_id == tid, Product.product_id.in_(product_ids)))
        session.execute(delete(Partner).where(Partner.tenant_id == tid, Partner.partner_id.in_(partner_ids)))

    return DemoSummary(
        tenant_id=str(tid),
        tenant_name=str(tenant.name),
        store_id=str(store_id),
        member_id=str(member_id),
    )


def main() -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="完整演示数据灌入 / 清理（兼容 PG FORCE RLS）")
    parser.add_argument("--tenant", default="", help="指定租户 UUID（默认最近一个）")
    parser.add_argument("--list", action="store_true", help="只列出库里的租户，不写库")
    parser.add_argument("--clean", action="store_true", help="仅清理该租户的演示数据")
    args = parser.parse_args()

    database = Database.from_url(_database_url())
    try:
        if args.tenant and not args.list:
            try:
                target_tid = uuid.UUID(args.tenant)
            except ValueError:
                print(f"租户 UUID 格式无效：{args.tenant}")
                return 1
            chosen = _load_tenant(database, target_tid)
            if chosen is None:
                print(f"未找到租户 {args.tenant}，请先运行 --list 查看可用租户。")
                return 1
        else:
            tenants = _list_tenants(database)
            if not tenants:
                print(
                    "库里还没有已登录的租户。\n"
                    "请确认小程序首页「系统自检」显示已登录（若仍为空，检查小程序是否连到了当前云托管环境）。"
                )
                return 1

            if args.list:
                print("库里的租户（最近创建的在最前）：")
                for item in tenants:
                    print(f"  {item.tenant_id}  {item.name}  {item.status}")
                return 0
            chosen = tenants[0]

        if args.clean:
            summary = clean_tenant(database, chosen)
            print(f"✓ 已清理租户 {summary.tenant_id}（{summary.tenant_name}）下的演示数据（真实业务数据已保留）。")
            return 0

        summary = seed_tenant(database, chosen)
        print("=" * 68)
        print(f"✓ 已为租户 {summary.tenant_id}（{summary.tenant_name}）灌入完整演示数据")
        print(f"  门店={summary.store_id}  操作员={summary.member_id}")
        print(
            f"  商品={summary.products}  批次={summary.batches}  往来单位={summary.partners}  "
            f"单据={summary.docs}  应收应付={summary.ar_ap}"
        )
        print(
            f"  待处理={summary.gaps}（缺进价/负库存/临期）  别名={summary.aliases}  "
            f"R3待办={summary.tasks}"
        )
        print("=" * 68)
        print("现在打开小程序即可直接体验：今天流水、货品筛选/批次、欠款核销、大屏管理台。")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
