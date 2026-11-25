"""部署自检探针：一条命令看清「容器到底连上了哪个库、隔离是否真的生效」。

为什么需要它
------------
「连没连上 PG」这件事，靠猜会浪费一整天。本项目踩过的坑全都是**静默失败**：

- ``DATABASE_URL`` 没配 → 落到内存 SQLite：服务能起、接口 200、重启数据全丢；
- 只配了云托管的 ``PGHOST/PGUSER/...`` 那套 libpq 变量 → 本项目**不读**，同上；
- 连上了 PG 但**用超级用户** → ``FORCE RLS`` 形同虚设，跨租户能互看（实测过）；
- 迁移没跑 → 表数 0，``/readyz`` 报 ``table_count_mismatch``。

所以把「该看的几个数」固定成脚本，部署后进容器跑一次即可，不用靠猜。

用法
----
容器内（工作目录 /app，DATABASE_URL 已注入）::

    python scripts/probe_env.py

本地::

    $env:DATABASE_URL="postgresql+psycopg://vil:vil_local@127.0.0.1:5432/vil"
    ..\\.venv\\Scripts\\python.exe scripts/probe_env.py

退出码：0 = 全部通过；1 = 有硬性项没通过（可直接当部署门禁用）。
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

#: 期望的业务表数（与 config.EXPECTED_TABLES 同口径；这里不 import app 以免依赖顺序）
EXPECTED_TABLES = 42
EXPECTED_RLS_TABLES = 40
EXPECTED_RLS_POLICIES = 42
EXPECTED_HEAD_REVISION = "0007_m0_ledger_redesign"


def _mask(url: str) -> str:
    """把连接串里的密码换成 ``***``（日志/截图可以外发）。"""
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", url)


def _as_psycopg_conninfo(url: str) -> str:
    """SQLAlchemy URL → psycopg conninfo（只做前缀替换，参数原样保留）。"""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _q(conn: Any, sql: str, default: Any = None) -> Any:
    """执行单值查询（失败返回默认值，探针不因单项失败而中断）。"""
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else default
    except Exception as exc:  # noqa: BLE001 - 探针要尽量把每一项都打出来
        print(f"    ! 查询失败: {sql} -> {type(exc).__name__}: {exc}")
        return default


def main() -> int:
    """打印连接与隔离自检结果；返回退出码。"""
    url = os.getenv("DATABASE_URL", "").strip()
    print("=" * 72)
    print("语音录入版进销存 · 部署自检探针")
    print("=" * 72)
    if not url:
        print("✗ DATABASE_URL 未设置：应用会落到内存 SQLite（无 RLS、重启丢数据）")
        return 1

    print(f"DATABASE_URL : {_mask(url)}")
    if url.startswith("sqlite"):
        print("✗ 当前是 SQLite：没有 DB 级 RLS，且容器重启即丢数据（仅允许本地/测试）")
        return 1

    try:
        import psycopg  # 局部导入：SQLite 场景下不强制依赖
    except ImportError:
        print("✗ 容器里没有 psycopg：镜像装错了（生产镜像必须带 psycopg[binary]）")
        return 1

    checks: list[tuple[str, bool, str]] = []
    try:
        conn = psycopg.connect(_as_psycopg_conninfo(url), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 连不上数据库：{type(exc).__name__}: {exc}")
        print("  排查顺序：安全组/白名单 → VPC 是否与云托管一致 → sslmode → 账号密码")
        return 1

    with conn:
        version = _q(conn, "show server_version", "?")
        database = _q(conn, "select current_database()", "?")
        user = _q(conn, "select current_user", "?")
        superuser = _q(conn, "select usesuper from pg_user where usename = current_user", None)
        bypass = _q(
            conn, "select rolbypassrls from pg_roles where rolname = current_user", None
        )
        max_conn = _q(conn, "show max_connections", "?")
        tables = _q(
            conn,
            "select count(*) from information_schema.tables "
            "where table_schema = 'public' and table_type = 'BASE TABLE' "
            "and table_name <> 'alembic_version'",
            0,
        )
        rls_enabled = _q(
            conn,
            "select count(*) from pg_tables where schemaname = 'public' and rowsecurity = true",
            0,
        )
        rls_forced = _q(
            conn,
            "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relrowsecurity and c.relforcerowsecurity",
            0,
        )
        policies = _q(
            conn, "select count(*) from pg_policies where schemaname = 'public'", 0
        )
        revision = _q(conn, "select version_num from alembic_version", "（未迁移）")

        print("\n--- 连接与版本 ---")
        print(f"server_version : {version}")
        print(f"database       : {database}")
        print(f"current_user   : {user}  (super={superuser}, bypassrls={bypass})")
        print(f"max_connections: {max_conn}   ← 每容器实例峰值 15 条，实例数 × 15 要留余量")

        print("\n--- 迁移与 RLS ---")
        print(f"alembic_version: {revision}")
        print(f"业务表         : {tables}（期望 {EXPECTED_TABLES}）")
        print(f"RLS 已启用     : {rls_enabled}（期望 {EXPECTED_RLS_TABLES}）")
        print(f"RLS 已 FORCE   : {rls_forced}（期望 {EXPECTED_RLS_TABLES}；缺 FORCE 时表 owner 会绕过）")
        print(f"策略总数       : {policies}（期望 {EXPECTED_RLS_POLICIES} = {EXPECTED_RLS_TABLES} 隔离 + 2 引导）")

        checks.append(("迁移已跑到 head", revision == EXPECTED_HEAD_REVISION, f"revision={revision}"))
        checks.append(
            ("业务表齐全", int(tables or 0) == EXPECTED_TABLES, f"tables={tables}")
        )
        checks.append(("RLS 已启用", int(rls_enabled or 0) == EXPECTED_RLS_TABLES, f"enabled={rls_enabled}"))
        checks.append(("RLS 已 FORCE", int(rls_forced or 0) == EXPECTED_RLS_TABLES, f"forced={rls_forced}"))
        checks.append((f"策略数 {EXPECTED_RLS_POLICIES}", int(policies or 0) == EXPECTED_RLS_POLICIES, f"policies={policies}"))
        checks.append(
            (
                "应用账号非超级用户（否则 RLS 白装）",
                superuser is False and bypass is False,
                f"super={superuser}, bypassrls={bypass}",
            )
        )

    print("\n--- 判定 ---")
    failed = 0
    for name, ok, detail in checks:
        print(f"{'✓' if ok else '✗'} {name}  ({detail})")
        if not ok:
            failed += 1

    print("=" * 72)
    if failed:
        print(f"✗ {failed} 项未通过：**不要**在这种情况下对外开服")
        return 1
    print("✓ 全部通过：数据库、迁移、RLS、账号权限都符合生产口径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
