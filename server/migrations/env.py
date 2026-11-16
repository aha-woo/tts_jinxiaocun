"""Alembic 迁移环境（双模式：在线 upgrade / 离线 `--sql` 渲染）。

为什么离线渲染是 T02 的**主证据**
--------------------------------
本机没有 PostgreSQL（无 Docker、无 psql），无法靠起库验证 RLS。因此把
``alembic upgrade head --sql`` 渲染出的 DDL 文本当成可断言的产物：
逐表断言 ``CREATE TABLE ... tenant_id``、``ENABLE ROW LEVEL SECURITY``、
``FORCE ROW LEVEL SECURITY``、``CREATE POLICY ... current_setting('app.tenant_id')``、
``UNIQUE (tenant_id, ...)``。这样即使没有 PG，也能证明 RLS 真的写进了迁移。

连接串优先级
------------
1. ``config.attributes["database_url"]``：测试/工具显式注入（最高优先级，便于隔离）；
2. 环境变量 ``DATABASE_URL``：云托管/CI 注入；
3. ``alembic.ini`` 的 ``sqlalchemy.url``：本地默认（PG，方案 A）。

**不要**在离线模式下调用 ``create_engine``：``context.configure(url=...)`` 只需要方言。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# 允许 `alembic -c server/alembic.ini ...` 在仓库根执行时也能 import app.*
_SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVER_ROOT))

from app.db.base import Base  # noqa: E402  - 必须先设置 sys.path
import app.models  # noqa: E402,F401  - 导入即注册全部 34 张表

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: alembic.ini 里的占位符（未配置真实库时使用）
PLACEHOLDER_URL = "postgresql+psycopg://vil:vil@localhost:5432/vil"

#: 本机无 PG 时的兜底（仅在线模式且显式要求时才用得到；默认仍是 PG 以便渲染 RLS）
SQLITE_FALLBACK_URL = "sqlite+pysqlite:///./vil_local.db"


def resolve_database_url() -> str:
    """按「attributes -> 环境变量 -> ini」优先级解析连接串。"""
    overridden = config.attributes.get("database_url")
    if overridden:
        return str(overridden)

    from_env = os.getenv("DATABASE_URL", "").strip()
    if from_env:
        return from_env

    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured:
        return configured

    return SQLITE_FALLBACK_URL


def run_migrations_offline() -> None:
    """离线模式：只渲染 SQL，不连库（CI 断言 RLS 的主路径）。"""
    url = resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        # PG 专用：让 `--sql` 输出可被 `psql -f` 直接执行
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行（本地 SQLite 冒烟 / 生产 PG）。"""
    url = resolve_database_url()
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                # SQLite 不支持 ALTER，需要 batch 模式（本项目首版只建表，留作后续 ALTER 的护栏）
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
