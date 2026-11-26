"""夜间三账对账与收摊总结巡检脚本。

用法：
    cd server
    python scripts/nightly_reconcile.py [--send-summary]

退出码：0 全部正常；2 发现三账账实不平。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.models.org import Member, Role, Store  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.services.closing_summary import build_closing_summary, send_closing_summary  # noqa: E402
from app.services.reconcile import reconcile_store  # noqa: E402
from app.tenant import rls  # noqa: E402


def _list_active_tenants(database: Database) -> list[Tenant]:
    """按 PG FORCE RLS 引导策略列出 active 租户。"""
    with database.session(bypass_scope=True) as s:
        rls.set_auth_bootstrap(s, True)
        raw_tids = list(
            s.execute(select(Member.tenant_id).order_by(Member.created_at.desc())).scalars()
        )
        rls.set_auth_bootstrap(s, False)

    tenants: list[Tenant] = []
    for tid in dict.fromkeys(raw_tids):
        with database.session(bypass_scope=True) as s:
            rls.set_tenant_guc(s, tid)
            tenant = s.get(Tenant, tid)
            if tenant is not None and str(tenant.status) == "active":
                tenants.append(tenant)
    tenants.sort(key=lambda item: item.created_at, reverse=True)
    return tenants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三账对账与收摊总结巡检")
    parser.add_argument("--send-summary", action="store_true", help="尝试发送微信订阅消息收摊总结")
    args = parser.parse_args(argv)

    settings = get_settings()
    database = Database.from_url(settings.database_url)
    has_mismatch = False

    tenants = _list_active_tenants(database)

    for tenant in tenants:
        with database.session(bypass_scope=True) as s:
            rls.set_tenant_guc(s, tenant.tenant_id)
            stores = list(
                s.execute(select(Store).where(Store.tenant_id == tenant.tenant_id)).scalars()
            )
            for store in stores:
                recon = reconcile_store(s, tenant_id=tenant.tenant_id, store_id=store.store_id)
                if not recon["ok"]:
                    has_mismatch = True
                print(
                    f"[reconcile] tenant={tenant.name} store={store.name} "
                    f"ok={recon['ok']} products={recon['checked_products']} "
                    f"neg={recon['negative_count']} provisional={recon['provisional_or_unknown_cost_lines']}"
                )
                if args.send_summary:
                    summary = build_closing_summary(s, tenant_id=tenant.tenant_id, store_id=store.store_id)
                    boss_row = s.execute(
                        select(Member)
                        .join(Role, (Role.tenant_id == Member.tenant_id) & (Role.member_id == Member.member_id))
                        .where(
                            Member.tenant_id == tenant.tenant_id,
                            Role.role_code == "boss",
                            Member.status == "active",
                        )
                        .limit(1)
                    ).scalar_one_or_none()
                    if boss_row is not None:
                        res = send_closing_summary(settings, openid=str(boss_row.wx_openid), summary=summary)
                        print(f"  [summary] sent={res['sent']} dry_run={res['dry_run']} text={summary['summary_text']}")

    database.dispose()
    return 2 if has_mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
