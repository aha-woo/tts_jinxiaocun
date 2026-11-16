"""真实 PostgreSQL 上的 RLS 交叉租户验证（**唯一能在「没有 PG」环境下跳过的文件**）。

跳过不是「算了」，而是把「这台机器做不到的事」和「已经做到的替代证据」写清楚：

- 本机环境事实：无 Docker、无 psql、无 PostgreSQL 实例 → 无法建库；
- 本机替代证据（默认 ``pytest -q`` 会跑）：
  1. ``tests/test_migration_rls_ddl.py``：``alembic upgrade head --sql`` 离线渲染，
     逐表断言 ``ENABLE`` / **``FORCE``** / ``CREATE POLICY ... current_setting('app.tenant_id')``，
     以及「唯一约束以 tenant_id 打头」「平台级表豁免」；
  2. ``tests/test_tenant_scope_no_rls.py``：SQLite 上双租户 A/B 交叉 + contextvar 并发隔离
     + 缓存前缀 + 导出不含跨租户；
- 本文件负责的**只有「真库行为」**：应用账号是否非 BYPASSRLS、表上
  ``relrowsecurity``/``relforcerowsecurity`` 是否真的为 true、策略是否真的在拦数据、
  无租户 GUC 时是否「一行都看不见」而不是报错。

在有 PG 的环境上跑：
    PG_TEST_URL='postgresql+psycopg://vil:vil@127.0.0.1:5432/vil_test' pytest -q
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text

import app.models  # noqa: F401
from app.db.base import PLATFORM_TABLES, Base

SERVER_ROOT = Path(__file__).resolve().parents[1]

PG_TEST_URL = os.getenv("PG_TEST_URL", "").strip()

SKIP_REASON = (
    "未设置 PG_TEST_URL：本机没有 PostgreSQL（无 Docker / 无 psql / 无本机实例），"
    "真实 RLS 行为必须在 PG 上复查。本机已用的替代证据："
    "tests/test_migration_rls_ddl.py（Alembic 离线 DDL 逐表断言 FORCE + policy 表达式 + "
    "唯一约束 tenant_id 打头）与 tests/test_tenant_scope_no_rls.py（SQLite 应用层隔离）。"
    "接上 PG 后执行：PG_TEST_URL='postgresql+psycopg://user:pwd@host:5432/db' pytest -q"
)

pytestmark = pytest.mark.skipif(not PG_TEST_URL, reason=SKIP_REASON)


def tenant_table_names() -> list[str]:
    """需要 RLS 的表（带 tenant_id 且非平台级）。"""
    return sorted(
        name
        for name, table in Base.metadata.tables.items()
        if "tenant_id" in table.c and name not in PLATFORM_TABLES
    )


def set_tenant(conn: Any, tenant_id: uuid.UUID) -> None:
    """事务级设置租户 GUC（等价 ``SET LOCAL``，事务结束自动复位）。"""
    conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Any]:
    """连上 PG 并把迁移跑到 head（含 RLS 三件套）。"""
    from alembic import command
    from alembic.config import Config

    config = Config(str(SERVER_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    config.attributes["database_url"] = PG_TEST_URL
    command.upgrade(config, "head")

    engine = create_engine(PG_TEST_URL, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


def insert_tenant(conn: Any, tenant_id: uuid.UUID) -> None:
    """插一行租户（先绑 GUC，RLS 的 WITH CHECK 才放行）。"""
    conn.execute(
        text(
            "INSERT INTO tenant (tenant_id, name, plan_code, status, quota_monthly, version) "
            "VALUES (:tid, :name, 'free', 'active', 200, 1) ON CONFLICT (tenant_id) DO NOTHING"
        ),
        {"tid": str(tenant_id), "name": f"RLS-{tenant_id.hex[:8]}"},
    )


def insert_doc(conn: Any, tenant_id: uuid.UUID, doc_no: str) -> None:
    """插一行单据（同 tenant 内 doc_no 唯一）。"""
    conn.execute(
        text(
            "INSERT INTO doc_header "
            "(doc_id, doc_no, intent_id, doc_type, biz_date, payment_status, "
            " total_amount_fen, status, created_by, tenant_id, store_id) "
            "VALUES (:doc_id, :doc_no, 'sale.create', 'sale', :biz_date, '已结', "
            " 0, 'confirmed', :created_by, :tid, :store_id) "
            "ON CONFLICT (doc_id) DO NOTHING"
        ),
        {
            "doc_id": str(uuid.uuid4()),
            "doc_no": doc_no,
            "biz_date": date(2026, 1, 1),
            "created_by": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "store_id": str(uuid.uuid4()),
        },
    )


def cleanup(engine: Any, tenant_ids: tuple[uuid.UUID, ...]) -> None:
    """清理本测试插入的数据（避免污染重复执行）。"""
    with engine.begin() as conn:
        for tenant_id in tenant_ids:
            set_tenant(conn, tenant_id)
            conn.execute(text("DELETE FROM doc_header WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
            conn.execute(text("DELETE FROM tenant WHERE tenant_id = :tid"), {"tid": str(tenant_id)})


def test_application_role_cannot_bypass_rls(pg_engine: Any) -> None:
    """应用账号必须既不是超级用户也不是 BYPASSRLS，否则 FORCE 之外仍会被绕过。"""
    with pg_engine.begin() as conn:
        row = conn.execute(
            text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "应用账号是超级用户：RLS 形同虚设"
    assert row.rolbypassrls is False, "应用账号带 BYPASSRLS：RLS 形同虚设"


def test_every_tenant_table_has_rls_enabled_and_forced(pg_engine: Any) -> None:
    """pg_class 上逐表核对：relrowsecurity 与 relforcerowsecurity 都必须为 true。"""
    with pg_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relkind = 'r' AND relname = ANY(:names)"
            ),
            {"names": tenant_table_names()},
        ).all()
    seen = {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in rows}
    missing = [name for name in tenant_table_names() if name not in seen]
    assert missing == [], f"库中没有这些表: {missing}"
    not_forced = [name for name, (enabled, forced) in seen.items() if not (enabled and forced)]
    assert not_forced == [], f"以下表未同时 ENABLE+FORCE RLS: {not_forced}"


def test_policies_carry_tenant_guc_expression(pg_engine: Any) -> None:
    """pg_policies：每张租户表都有策略，且表达式引用 app.tenant_id。"""
    with pg_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename, policyname, qual, with_check FROM pg_policies "
                "WHERE schemaname = current_schema() AND tablename = ANY(:names)"
            ),
            {"names": tenant_table_names()},
        ).all()
    by_table: dict[str, list[Any]] = {}
    for row in rows:
        by_table.setdefault(row.tablename, []).append(row)

    problems: list[str] = []
    for table_name in tenant_table_names():
        policies = by_table.get(table_name, [])
        isolation = [item for item in policies if item.policyname == f"{table_name}_tenant_isolation"]
        if len(isolation) != 1:
            problems.append(f"{table_name}: 租户策略数 {len(isolation)}")
            continue
        policy = isolation[0]
        if "app.tenant_id" not in (policy.qual or ""):
            problems.append(f"{table_name}: USING 未引用 app.tenant_id")
        if "app.tenant_id" not in (policy.with_check or ""):
            problems.append(f"{table_name}: WITH CHECK 未引用 app.tenant_id")
    assert problems == [], problems


def test_cross_tenant_rows_are_invisible(pg_engine: Any) -> None:
    """A 绑 GUC 后：查不到 B 的任何行，即使 WHERE 直指 B 的 tenant_id。"""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    try:
        with pg_engine.begin() as conn:
            for tenant_id, doc_no in ((tenant_a, "RLS-DOC-A"), (tenant_b, "RLS-DOC-B")):
                set_tenant(conn, tenant_id)
                insert_tenant(conn, tenant_id)
                insert_doc(conn, tenant_id, doc_no)

        with pg_engine.begin() as conn:
            set_tenant(conn, tenant_a)
            visible = conn.execute(
                text("SELECT tenant_id FROM doc_header WHERE doc_no LIKE 'RLS-DOC-%'")
            ).all()
            assert {str(row.tenant_id) for row in visible} == {str(tenant_a)}

            targeted = conn.execute(
                text("SELECT count(*) FROM doc_header WHERE tenant_id = :tid"),
                {"tid": str(tenant_b)},
            ).scalar_one()
            assert targeted == 0, "直指他租户 tenant_id 仍能读到数据：RLS 没生效"
    finally:
        cleanup(pg_engine, (tenant_a, tenant_b))


def test_no_tenant_guc_sees_nothing_but_does_not_error(pg_engine: Any) -> None:
    """没有租户上下文时必须「安静地看不见」，而不是抛 unknown GUC 把服务打挂。"""
    with pg_engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM doc_header")).scalar_one()
    assert count == 0
