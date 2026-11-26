"""测试 ``server/scripts/seed_rich_demo.py`` 的幂等性、报表示数与安全清理。"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.db.session import Database
from app.domain.report_service import ReportService
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import PersonalDict
from app.models.doc import ArAp, DocHeader, DocLine, Payment
from app.models.inventory import Inventory, StockBatch
from app.models.ops import Gap, PendingTask
from app.models.org import Member, Role, Store
from app.models.partner import Partner
from app.models.tenant import Tenant
from app.tenant.context import TenantContext
from app.utils.date_utils import from_iso_date, today_in_tz
from scripts.seed_rich_demo import clean_tenant, seed_tenant


def _setup_tenant_db() -> tuple[Database, Tenant, Store, Member]:
    database = Database.from_url("sqlite+pysqlite:///:memory:")
    database.create_all()
    tenant = Tenant(name="王姐测试便利店")
    with database.session(bypass_scope=True) as session:
        session.add(tenant)
        session.flush()
        store = Store(tenant_id=tenant.tenant_id, name="主店", is_main=True)
        member = Member(tenant_id=tenant.tenant_id, wx_openid="seed-test-openid", name="王姐")
        session.add(store)
        session.add(member)
        session.flush()
        session.add(
            Role(
                tenant_id=tenant.tenant_id,
                member_id=member.member_id,
                store_id=store.store_id,
                role_code="boss",
            )
        )
        # 预置一条「真实业务商品」，验证 --clean 绝不误删非演示数据
        real_prod = Product(
            tenant_id=tenant.tenant_id,
            product_id=uuid.uuid4(),
            name="真实自有商品",
            spec="500g",
            base_unit="袋",
            last_purchase_price_fen=800,
            last_sale_price_fen=1200,
        )
        session.add(real_prod)
        session.flush()
        session.add(
            Inventory(
                tenant_id=tenant.tenant_id,
                store_id=store.store_id,
                product_id=real_prod.product_id,
                qty_base=Decimal("10"),
            )
        )
    return database, tenant, store, member


def test_seed_rich_demo_is_idempotent_and_clean_preserves_real_data() -> None:
    database, tenant, store, member = _setup_tenant_db()
    ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        store_id=store.store_id,
        member_id=member.member_id,
        role_code="boss",
        tenant_version=tenant.version,
    )
    try:
        summary = seed_tenant(database, tenant)
        assert summary.products == 8
        assert summary.docs == 11

        # 第二次执行必须完全幂等，不产生重复行
        seed_tenant(database, tenant)

        with database.session(ctx) as session:
            assert session.scalar(select(func.count()).select_from(Product).where(Product.tenant_id == tenant.tenant_id)) == 9
            assert session.scalar(select(func.count()).select_from(UnitConversion).where(UnitConversion.tenant_id == tenant.tenant_id)) == 6
            assert session.scalar(select(func.count()).select_from(StockBatch).where(StockBatch.tenant_id == tenant.tenant_id)) == 5
            assert session.scalar(select(func.count()).select_from(Partner).where(Partner.tenant_id == tenant.tenant_id)) == 5
            assert session.scalar(select(func.count()).select_from(DocHeader).where(DocHeader.tenant_id == tenant.tenant_id)) == 11
            assert session.scalar(select(func.count()).select_from(DocLine).where(DocLine.tenant_id == tenant.tenant_id)) == 12
            assert session.scalar(select(func.count()).select_from(ArAp).where(ArAp.tenant_id == tenant.tenant_id)) == 5
            assert session.scalar(select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant.tenant_id)) == 1
            assert session.scalar(select(func.count()).select_from(Gap).where(Gap.tenant_id == tenant.tenant_id)) == 3
            assert session.scalar(select(func.count()).select_from(ProductAlias).where(ProductAlias.tenant_id == tenant.tenant_id)) == 4
            assert session.scalar(select(func.count()).select_from(PersonalDict).where(PersonalDict.tenant_id == tenant.tenant_id)) == 4
            assert session.scalar(select(func.count()).select_from(PendingTask).where(PendingTask.tenant_id == tenant.tenant_id)) == 1

            report = ReportService(session).today(
                tenant_id=tenant.tenant_id,
                store_id=store.store_id,
                biz_date=from_iso_date(today_in_tz()),
            )
            assert report.sales_fen == 29000
            assert report.purchase_fen == 7200
            assert report.receivable_open_fen == 189000
            assert report.payable_open_fen == 115200
            assert len(ReportService(session).stock_of(tenant_id=tenant.tenant_id, store_id=store.store_id)) == 9
            assert len(ReportService(session).debts(tenant_id=tenant.tenant_id)) == 5

        # 清理只删演示数据，保留真实商品
        clean_tenant(database, tenant)
        with database.session(ctx) as session:
            remaining = session.execute(select(Product).where(Product.tenant_id == tenant.tenant_id)).scalars().all()
            assert [p.name for p in remaining] == ["真实自有商品"]
            assert session.scalar(select(func.count()).select_from(DocHeader).where(DocHeader.tenant_id == tenant.tenant_id)) == 0
            assert session.scalar(select(func.count()).select_from(ArAp).where(ArAp.tenant_id == tenant.tenant_id)) == 0
            assert session.scalar(select(func.count()).select_from(Gap).where(Gap.tenant_id == tenant.tenant_id)) == 0
            assert session.scalar(select(func.count()).select_from(PendingTask).where(PendingTask.tenant_id == tenant.tenant_id)) == 0
    finally:
        database.dispose()
