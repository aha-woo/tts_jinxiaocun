"""T02 主证据：Alembic **离线渲染** DDL 逐表断言（本机无 PostgreSQL 时的验收路径）。

为什么这样做
------------
本机没有 Docker / psql / PostgreSQL，无法「起库看 RLS」。但 RLS 是否真的落进迁移，
本质上是**迁移产物**的问题：``alembic upgrade head --sql`` 渲染出的 DDL 就是
部署时会送到 PG 上执行的文本。因此把这段文本当成可断言对象：

- 34 张表是否都建出来（含 tenant_id 列）；
- 32 张租户表是否都有 ``ENABLE`` + **``FORCE``** + ``CREATE POLICY`` 三件套
  （缺 ``FORCE`` 时表 owner 会绕过 RLS，是最常见的假隔离）；
- 策略总数是否等于 ``config.EXPECTED_RLS_POLICIES``（32 条租户隔离 + 2 条引导反查 = 34）；
- 策略表达式是否引用 ``current_setting('app.tenant_id'``；
- 平台级表（``goods_item`` / ``clarifier_templates``）是否**豁免**；
- 可空 tenant 的三层表是否用 ``tenant_id IS NULL OR ...`` 放行平台层行；
- 唯一约束是否**以 tenant_id 打头**（跨租户撞号的根因）；
- 同一份迁移在 SQLite 方言下**不渲染**任何 RLS 语句（不假装 SQLite 有 RLS）；
- 种子是否从 ``goods.json`` 读（价格/额度不硬编码）。
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config

import app.models  # noqa: F401  - 必须在模块导入期完成模型注册（parametrize 依赖它）
from app.config import EXPECTED_RLS_POLICIES, EXPECTED_TABLES
from app.db.base import NULLABLE_TENANT_TABLES, PLATFORM_TABLES, Base

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
PG_RENDER_URL = "postgresql+psycopg://render_only:render_only@localhost:5432/render_only"
SQLITE_RENDER_URL = "sqlite+pysqlite:///:memory:"


def render_sql(url: str) -> str:
    """离线渲染 ``upgrade head`` 的全部 SQL（不连库）。"""
    config = Config(str(SERVER_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    # attributes 优先级最高（见 migrations/env.py），保证不受外部 DATABASE_URL 干扰
    config.attributes["database_url"] = url
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    rendered = buffer.getvalue()
    assert rendered.strip(), "离线渲染结果为空：Alembic 配置或 env.py 有问题"
    return rendered


@pytest.fixture(scope="module")
def pg_sql() -> str:
    """PG 方言渲染结果（主证据）。"""
    return render_sql(PG_RENDER_URL)


@pytest.fixture(scope="module")
def sqlite_sql() -> str:
    """SQLite 方言渲染结果（验证不会渲染 RLS 语句）。"""
    return render_sql(SQLITE_RENDER_URL)


def all_table_names() -> list[str]:
    """模型声明的全部表名（不含 alembic_version）。"""
    return sorted(Base.metadata.tables)


def tenant_table_names() -> list[str]:
    """需要 RLS 的表：带 tenant_id 且不在平台级白名单（含根表 ``tenant``）。"""
    return sorted(
        name
        for name, table in Base.metadata.tables.items()
        if "tenant_id" in table.c and name not in PLATFORM_TABLES
    )


def create_table_block(sql: str, table_name: str) -> str:
    """抽取某张表的 ``CREATE TABLE ...;`` 全文。"""
    match = re.search(rf"^CREATE TABLE {table_name} \(.*?\n\);", sql, re.M | re.S)
    assert match is not None, f"渲染结果里找不到 {table_name} 的建表语句"
    return match.group(0)


def table_statements(sql: str, table_name: str) -> list[str]:
    """抽取某张表的 RLS 语句（ENABLE / FORCE / POLICY）。"""
    pattern = re.compile(
        rf"^(?:ALTER TABLE {table_name}\b.*|CREATE POLICY .* ON {table_name}\b.*);$",
        re.M,
    )
    return [line for line in pattern.findall(sql)]


# --------------------------------------------------------------------------- 表数


def test_all_34_tables_are_created(pg_sql: str) -> None:
    """34 张表（架构 §4.3.2 全量）+ alembic_version。"""
    created = set(re.findall(r"^CREATE TABLE (\w+)", pg_sql, re.M))
    expected = set(all_table_names())
    assert expected == set(all_table_names())  # 防手滑
    assert len(expected) == EXPECTED_TABLES == 34
    assert expected <= created, f"缺失建表语句: {sorted(expected - created)}"
    assert created - expected == {"alembic_version"}


def test_tenant_table_count_matches_expectation() -> None:
    """32 张表启用 RLS = 31 张业务表 + 根表 tenant（根表也强制隔离，不搞特权）。"""
    names = tenant_table_names()
    assert len(names) == 32
    assert "tenant" in names
    assert PLATFORM_TABLES.isdisjoint(names)
    assert len(Base.metadata.tables) - len(names) == 2  # goods_item / clarifier_templates


# --------------------------------------------------------------- 逐表 RLS 三件套


@pytest.mark.parametrize("table_name", tenant_table_names())
def test_each_tenant_table_has_rls_triplet(pg_sql: str, table_name: str) -> None:
    """逐表断言：tenant_id 列 + ENABLE + **FORCE** + POLICY(引用 app.tenant_id)。"""
    block = create_table_block(pg_sql, table_name)
    assert re.search(r"\btenant_id UUID", block), f"{table_name} 缺少 tenant_id 列"

    statements = table_statements(pg_sql, table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;" in statements, (
        f"{table_name} 未启用 RLS"
    )
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;" in statements, (
        f"{table_name} 缺 FORCE：表 owner 会绕过 RLS"
    )

    policies = [line for line in statements if line.startswith("CREATE POLICY")]
    isolation = [line for line in policies if f"CREATE POLICY {table_name}_tenant_isolation" in line]
    assert len(isolation) == 1, f"{table_name} 期望恰好 1 条租户隔离策略，实际 {policies}"
    # 唯一允许的额外策略：两张「鸡生蛋」引导表各一条只读反查策略
    # （member → member_login_lookup；virtual_order → xpay_deliver_lookup）
    extras = [line for line in policies if line not in isolation]
    if table_name == "member":
        assert len(extras) == 1 and "member_login_lookup" in extras[0], extras
    elif table_name == "virtual_order":
        assert len(extras) == 1 and "xpay_deliver_lookup" in extras[0], extras
    else:
        assert extras == [], f"{table_name} 出现未预期的额外策略: {extras}"

    policy = isolation[0]
    assert f"ON {table_name} " in policy
    assert "current_setting('app.tenant_id'" in policy, f"{table_name} 策略未引用租户 GUC"
    assert "WITH CHECK" in policy, f"{table_name} 策略缺 WITH CHECK：写入不受约束"


def test_rls_triplet_counts(pg_sql: str) -> None:
    """32 张表 × 2 条 ALTER + 32 条租户策略 + 2 条引导策略 = EXPECTED_RLS_POLICIES。"""
    assert len(re.findall(r"ENABLE ROW LEVEL SECURITY;", pg_sql)) == 32
    assert len(re.findall(r"FORCE ROW LEVEL SECURITY;", pg_sql)) == 32
    policy_count = len(re.findall(r"^CREATE POLICY", pg_sql, re.M))
    assert policy_count == EXPECTED_RLS_POLICIES, (
        f"策略总数 {policy_count} ≠ EXPECTED_RLS_POLICIES={EXPECTED_RLS_POLICIES}"
        "（加策略时必须同步改 config.py 的那一个常量，别只改迁移）"
    )
    assert policy_count == 34, "32 条租户隔离 + member_login_lookup + xpay_deliver_lookup"


def test_platform_tables_are_exempt_from_rls(pg_sql: str) -> None:
    """平台级表无 tenant_id、不参与 RLS（但仍有唯一约束）。"""
    for table_name in ("goods_item", "clarifier_templates"):
        assert table_name in PLATFORM_TABLES
        block = create_table_block(pg_sql, table_name)
        assert "tenant_id" not in block, f"{table_name} 不应有 tenant_id"
        assert table_statements(pg_sql, table_name) == [], f"{table_name} 不应有 RLS 语句"


def test_nullable_tenant_policies_allow_platform_rows(pg_sql: str) -> None:
    """三层表（平台层 + 租户层并存）策略必须放行 tenant_id IS NULL。"""
    for table_name in sorted(NULLABLE_TENANT_TABLES):
        policies = [line for line in table_statements(pg_sql, table_name) if line.startswith("CREATE POLICY")]
        assert len(policies) == 1
        assert "tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id'" in policies[0], (
            f"{table_name} 的策略会挡掉平台层行：{policies[0]}"
        )


def test_member_login_lookup_policy_exists(pg_sql: str) -> None:
    """RLS 鸡生蛋：openid -> tenant_id 反查有专用只读策略，且受 bootstrap 开关约束。"""
    assert "CREATE POLICY member_login_lookup ON member FOR SELECT" in pg_sql
    assert "current_setting('app.auth_bootstrap', true) = 'on'" in pg_sql
    # 不能顺手放开写权限
    assert "member_login_lookup ON member FOR INSERT" not in pg_sql
    assert "member_login_lookup ON member FOR ALL" not in pg_sql


def test_xpay_deliver_lookup_policy_exists(pg_sql: str) -> None:
    """RLS 鸡生蛋（第二处）：out_trade_no -> tenant_id 反查有专用只读策略。

    发货推送没有用户 JWT，必须先按单号找到租户才能写额度；这条策略就是那一步的
    唯一入口。它必须同时满足「无租户上下文」+「``app.deliver_bootstrap = on``」，
    且只授 SELECT —— 否则发货路径会变成一个可以读别人订单的后门。
    """
    assert "CREATE POLICY xpay_deliver_lookup ON virtual_order FOR SELECT" in pg_sql, (
        "发货推送的租户反查策略缺失：PG 上将查不到任何订单，发货必然 404"
    )
    assert "current_setting('app.deliver_bootstrap', true) = 'on'" in pg_sql
    # 与登录引导是**两个独立开关**：发货策略里不允许出现 auth_bootstrap
    deliver_policy = next(
        line for line in pg_sql.splitlines() if "xpay_deliver_lookup ON virtual_order" in line
    )
    assert "app.deliver_bootstrap" in deliver_policy
    assert "app.auth_bootstrap" not in deliver_policy
    # 只读：不得放开写权限
    assert "xpay_deliver_lookup ON virtual_order FOR INSERT" not in pg_sql
    assert "xpay_deliver_lookup ON virtual_order FOR ALL" not in pg_sql
    assert "xpay_deliver_lookup ON virtual_order FOR UPDATE" not in pg_sql


# ------------------------------------------------------------------ 唯一约束/索引


def test_unique_constraints_start_with_tenant_id(pg_sql: str) -> None:
    """租户内唯一：唯一约束的**第一列必须是 tenant_id**（否则跨租户互相撞号）。"""
    offenders: list[str] = []
    for table_name in tenant_table_names():
        block = create_table_block(pg_sql, table_name)
        for columns in re.findall(r"UNIQUE \(([^)]+)\)", block):
            first = columns.split(",")[0].strip()
            if first != "tenant_id":
                offenders.append(f"{table_name}: UNIQUE ({columns})")
    assert offenders == [], f"以下唯一约束不以 tenant_id 打头: {offenders}"


def test_key_unique_constraints_present(pg_sql: str) -> None:
    """关键幂等/唯一键逐条点名（虚拟支付与语音扣次的核心防线）。"""
    expected = {
        "deliver_event": "uq_deliver_event_tenant_id_out_trade_no",
        "virtual_order": "uq_virtual_order_tenant_id_out_trade_no",
        "usage_ledger": "uq_usage_ledger_tenant_id_session_id_event_type",
        "usage_quota": "uq_usage_quota_tenant_id_period",
        "doc_header": "uq_doc_header_tenant_id_doc_no",
        "payment": "uq_payment_tenant_id_idempotency_key",
        "export_job": "uq_export_job_tenant_id_idempotency_key",
        "inventory": "uq_inventory_tenant_id_store_id_product_id",
    }
    for table_name, constraint in expected.items():
        block = create_table_block(pg_sql, table_name)
        assert constraint in block, f"{table_name} 缺少 {constraint}"
        assert re.search(rf"UNIQUE \(tenant_id,", block)


def test_amount_columns_are_integer_fen(pg_sql: str) -> None:
    """金额一律整数分（BIGINT/int），禁止浮点（避免 0.1+0.2 类误差）。"""
    for table_name in ("doc_header", "doc_line", "ar_ap", "payment", "virtual_order"):
        block = create_table_block(pg_sql, table_name)
        assert "FLOAT" not in block.upper() and "REAL" not in block.upper()
        assert re.search(r"\w+_fen (BIGINT|INTEGER)", block), f"{table_name} 缺少 *_fen 整数金额列"


# --------------------------------------------------------------- 方言差异（不假装）


def test_doc_header_marks_reversal_docs(pg_sql: str) -> None:
    """``doc_header.reverses_doc_id``：反做单必须能自报「我撤的是谁」。

    没有这一列时：反做单继承原单 ``intent_id``，「+48 还是 −48」在单头上丢失
    → 撤销的撤销再减一次（240→192→144），日报也把反做单当成又一笔采购。
    """
    block = create_table_block(pg_sql, "doc_header")
    assert re.search(r"\breverses_doc_id UUID", block), "doc_header 缺少 reverses_doc_id 列"
    # 正常单据为 NULL：不能设成 NOT NULL（否则每张单都得指向一张单）
    assert not re.search(r"reverses_doc_id UUID NOT NULL", block)
    # 唯一约束/索引仍以 tenant_id 打头（不许因为加列破坏租户内唯一）
    for columns in re.findall(r"UNIQUE \(([^)]+)\)", block):
        assert columns.split(",")[0].strip() == "tenant_id", columns


def test_sqlite_render_contains_no_rls_statements(sqlite_sql: str) -> None:
    """SQLite 方言下迁移**不渲染** RLS —— 不假装 SQLite 有行级安全。"""
    assert "ROW LEVEL SECURITY" not in sqlite_sql
    assert "CREATE POLICY" not in sqlite_sql
    created = set(re.findall(r"^CREATE TABLE (\w+)", sqlite_sql, re.M))
    assert set(all_table_names()) <= created


# ------------------------------------------------------------------------- 种子


def test_seed_goods_come_from_goods_json(pg_sql: str) -> None:
    """道具种子必须来自 ``packages/shared/contracts/goods.json``（单一真相源）。"""
    goods_doc = json.loads(
        (REPO_ROOT / "packages" / "shared" / "contracts" / "goods.json").read_text(encoding="utf-8")
    )
    expected = {item["goods_id"]: int(item["price_fen"]) for item in goods_doc["goods"]}
    assert expected, "goods.json 为空，种子无从谈起"

    inserts = re.findall(r"^INSERT INTO goods_item .*?VALUES \((.*)\);$", pg_sql, re.M)
    assert len(inserts) == len(expected), f"道具种子条数不匹配：{len(inserts)} vs {len(expected)}"
    seeded = {row.split(",")[0].strip().strip("'") for row in inserts}
    assert seeded == set(expected)
    for goods_id, price_fen in expected.items():
        assert re.search(rf"'{goods_id}'.*?, {price_fen},", pg_sql), (
            f"{goods_id} 的价格 {price_fen} 未来自 goods.json"
        )


def test_seed_platform_rows_have_null_tenant_id(pg_sql: str) -> None:
    """平台层种子行的 tenant_id 必须是 NULL（RLS 靠 IS NULL 放行）。"""
    for statement in re.findall(r"^INSERT INTO template_rule .*;$", pg_sql, re.M):
        assert statement.rstrip().endswith(", NULL);"), statement
    for statement in re.findall(r"^INSERT INTO num_normalization_rules .*;$", pg_sql, re.M):
        assert statement.rstrip().endswith(", NULL);"), statement


def test_migration_files_exist_in_expected_location() -> None:
    """迁移目录放在 server/migrations（不是 app/migrations），并含两个版本。"""
    versions = SERVER_ROOT / "migrations" / "versions"
    assert (SERVER_ROOT / "alembic.ini").is_file()
    assert (SERVER_ROOT / "migrations" / "env.py").is_file()
    assert (SERVER_ROOT / "migrations" / "script.py.mako").is_file()
    names = sorted(path.name for path in versions.glob("*.py"))
    assert names == ["0001_initial.py", "0002_seed.py"]


def test_upgrade_head_runs_forward_on_sqlite(tmp_path: Path) -> None:
    """在线 ``alembic upgrade head`` 必须真的能正向跑通（本地 SQLite 文件库）。

    离线 ``--sql`` 只证明「能渲染」，这里证明「能执行」：建表 + 种子一起落地，
    34 张业务表齐全、道具条数与 goods.json 一致。
    """
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "forward.db"
    url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    config = AlembicConfig(str(SERVER_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    config.attributes["database_url"] = url

    alembic_command.upgrade(config, "head")

    engine = create_engine(url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        assert set(all_table_names()) <= tables
        assert "alembic_version" in tables
        with engine.connect() as connection:
            goods = connection.execute(text("SELECT count(*) FROM goods_item")).scalar_one()
            rules = connection.execute(text("SELECT count(*) FROM template_rule")).scalar_one()
            clarifiers = connection.execute(
                text("SELECT count(*) FROM clarifier_templates")
            ).scalar_one()
    finally:
        engine.dispose()

    goods_doc = json.loads(
        (REPO_ROOT / "packages" / "shared" / "contracts" / "goods.json").read_text(encoding="utf-8")
    )
    assert goods == len(goods_doc["goods"])
    assert rules > 0 and clarifiers > 0


@pytest.fixture(autouse=True)
def _ensure_registry() -> Iterator[None]:
    """导入模型包（确保 Base.metadata 完整，避免子集视图）。"""
    import app.models  # noqa: F401

    yield
