"""本地演示数据：把「可乐」准备成能直接说人话的状态。**仅本地开发用。**

为什么需要它
------------
语音建商品（「新建商品可乐」）时基本单位默认是 **个**，而解析词表只装载
``status='approved'`` 的换算 —— 于是接着说「进了两箱可乐」会缺换算系数：

- 明细被标 ``pending`` → 确认被守卫拦下（这是**对的**，不该把 2 箱记成 2 个）；
- 提示让用户「补齐换算系数」，但目前**没有审批入口**（见 ``docs/03-遗留问题.md`` P1-3b）。

演示前跑一次本脚本，把商品与换算准备好，就能按最自然的话演示。

用法::

    cd server
    export DATABASE_URL="sqlite+pysqlite:///./dev.db"
    python scripts/demo_seed.py                 # 给最近登录的那个租户准备
    python scripts/demo_seed.py --tenant <id>   # 指定租户
    python scripts/demo_seed.py --list          # 只列租户，不写库

写库范围：只补 ``product`` 与 ``unit_conversion`` 两张表，**不动单据/库存**，
可以重复执行（幂等）。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

#: 让 `python scripts/demo_seed.py` 能 import 到 app（不加 -m 也能跑）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import Database  # noqa: E402
from app.models.catalog import Product, UnitConversion  # noqa: E402
from app.models.org import Member  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.tenant import rls  # noqa: E402

#: 演示商品：(名称, 规格, 基本单位, 进货价分, 售价分, 换算[(从单位, 到单位, 系数)])
DEMO_PRODUCTS: tuple[tuple[str, str, str, int, int, tuple[tuple[str, str, str], ...]], ...] = (
    ("可乐", "500ml", "瓶", 150, 250, (("箱", "瓶", "24"),)),
    ("矿泉水", "550ml", "瓶", 100, 200, (("箱", "瓶", "24"),)),
)


def _database_url() -> str:
    """取库地址（与后端一致：``DATABASE_URL`` 环境变量）。"""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "没设置 DATABASE_URL。请先和后端用同一个库，例如：\n"
            '  export DATABASE_URL="sqlite+pysqlite:///./dev.db"'
        )
    return url


def _tenants(database: Database) -> list[Tenant]:
    """库里的租户（按 PG FORCE RLS 引导策略读取，最近登录的在最前面）。"""
    with database.session(bypass_scope=True) as session:
        rls.set_auth_bootstrap(session, True)
        raw_tids = list(
            session.execute(select(Member.tenant_id).order_by(Member.created_at.desc())).scalars()
        )
        rls.set_auth_bootstrap(session, False)

    tenants: list[Tenant] = []
    for tid in dict.fromkeys(raw_tids):
        with database.session(bypass_scope=True) as session:
            rls.set_tenant_guc(session, tid)
            tenant = session.get(Tenant, tid)
            if tenant is not None:
                tenants.append(tenant)
    tenants.sort(key=lambda item: item.created_at, reverse=True)
    return tenants


def _upsert_product(
    session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    spec: str,
    base_unit: str,
    purchase_fen: int,
    sale_fen: int,
) -> tuple[Product, bool]:
    """按 (租户, 名称) 建或更新商品，并把基本单位/价格校正到演示值。"""
    product = session.execute(
        select(Product)
        .where(Product.tenant_id == tenant_id, Product.name == name)
        .limit(1)
    ).scalar_one_or_none()
    created = product is None
    if product is None:
        product = Product(
            tenant_id=tenant_id,
            product_id=uuid.uuid4(),
            name=name,
            spec=spec,
            base_unit=base_unit,
            last_purchase_price_fen=purchase_fen,
            last_sale_price_fen=sale_fen,
        )
        session.add(product)
    else:
        # 演示要的是「说箱就能算」，所以把基本单位与价格校正过来
        product.spec = spec
        product.base_unit = base_unit
        product.last_purchase_price_fen = purchase_fen
        product.last_sale_price_fen = sale_fen
    session.flush()
    return product, created


def _upsert_conversion(
    session,
    *,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    from_unit: str,
    to_unit: str,
    ratio: str,
) -> bool:
    """建或修正换算，并**直接置为 approved**（演示要立刻生效）。"""
    row = session.execute(
        select(UnitConversion)
        .where(
            UnitConversion.tenant_id == tenant_id,
            UnitConversion.product_id == product_id,
            UnitConversion.from_unit == from_unit,
            UnitConversion.to_unit == to_unit,
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        session.add(
            UnitConversion(
                tenant_id=tenant_id,
                product_id=product_id,
                from_unit=from_unit,
                to_unit=to_unit,
                ratio=Decimal(ratio),
                status="approved",
            )
        )
        session.flush()
        return True
    row.ratio = Decimal(ratio)
    row.status = "approved"
    session.flush()
    return False


def main() -> int:
    """入口：准备演示数据并打印「照着说」的剧本。"""
    parser = argparse.ArgumentParser(description="本地演示数据准备（仅开发用）")
    parser.add_argument("--tenant", default="", help="租户 id（缺省用最近登录的那个）")
    parser.add_argument("--list", action="store_true", help="只列出库里的租户")
    args = parser.parse_args()

    database = Database.from_url(_database_url())
    try:
        tenants = _tenants(database)
        if not tenants:
            print(
                "库里还没有租户。先在本地登录一次（小程序点一下，或）：\n"
                '  curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login '
                '-H "Content-Type: application/json" -d \'{"code":"demo"}\''
            )
            return 1

        if args.list:
            print("库里的租户（最近登录的在最前）：")
            for tenant in tenants:
                print(f"  {tenant.tenant_id}  {tenant.name}  {tenant.status}")
            return 0

        if args.tenant:
            chosen = next((t for t in tenants if str(t.tenant_id) == args.tenant), None)
            if chosen is None:
                print(f"没找到租户 {args.tenant}；用 --list 看看有哪些")
                return 1
        else:
            chosen = tenants[0]

        tenant_id = chosen.tenant_id
        print(f"为租户 {tenant_id}（{chosen.name}）准备演示数据…")
        with database.session(bypass_scope=True) as session:
            rls.set_tenant_guc(session, tenant_id)
            for name, spec, base_unit, purchase, sale, conversions in DEMO_PRODUCTS:
                product, created = _upsert_product(
                    session,
                    tenant_id=tenant_id,
                    name=name,
                    spec=spec,
                    base_unit=base_unit,
                    purchase_fen=purchase,
                    sale_fen=sale,
                )
                for from_unit, to_unit, ratio in conversions:
                    _upsert_conversion(
                        session,
                        tenant_id=tenant_id,
                        product_id=product.product_id,
                        from_unit=from_unit,
                        to_unit=to_unit,
                        ratio=ratio,
                    )
                print(
                    f"  {'新建' if created else '更新'}「{name} {spec}」"
                    f"基本单位 {base_unit}，"
                    + "，".join(f"1{from_u}={r}{to_u}" for from_u, to_u, r in conversions)
                )
    finally:
        database.dispose()

    print(
        "\n准备好啦。现在可以照着说（小程序里打字或语音都行）：\n"
        "  1) 进了两箱可乐，一件三十五         → 出确认卡，确认后库存 48 瓶\n"
        "  2) 卖了五瓶可乐，一瓶两块五         → 出确认卡，确认后库存 43 瓶\n"
        "  3) 今天卖了多少                     → 直接回答，不出卡\n"
        "  4) 撤销上一单                       → 反做上一张单，原单保留\n"
        "  5) 可乐别名叫“快乐水”               → 教说法，确认后下一句就能用\n"
        "\n注意：本机 LLM 是 fake（没有真实大模型），所以「更正上一单」这类\n"
        "必须靠模型的意图在本地会走不通（返回 failed），这不是代码坏了。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
