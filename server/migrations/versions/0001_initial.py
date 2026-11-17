"""0001 initial：34 张表 + PostgreSQL RLS 三件套。

设计要点
--------
1. **建表不手抄 DDL**：直接 ``Base.metadata.create_all(bind=op.get_bind())``，
   让迁移 DDL 与 ORM 模型**永远同源**（避免「模型加了列、迁移忘了加」的经典事故）。
   Alembic 的离线 ``--sql`` 模式把 bind 换成 ``MockConnection``，同样能渲染出完整
   建表语句，因此**无需连库**即可断言 DDL（本机无 PostgreSQL 时的主证据）。
2. **RLS 只在 PostgreSQL 上落地**：``ALTER ... ENABLE/FORCE ROW LEVEL SECURITY`` 与
   ``CREATE POLICY`` 是 PG 特性；SQLite 上跳过并**不假装已隔离**
   （``app/tenant/rls.py::capabilities()`` 会返回 ``rls_enabled=False``）。
3. **三类表的 RLS 策略不同**：
   - 普通业务表（31 张）：``tenant_id = current_setting('app.tenant_id')::uuid``；
   - tenant_id 可空的三层表（template_rule / template_candidate /
     num_normalization_rules）：``tenant_id IS NULL OR tenant_id = current_setting(...)``，
     否则平台层模板会被自己挡掉；
   - 平台级表（goods_item / clarifier_templates）：**无 tenant_id，豁免 RLS**。
4. **鸡生蛋的两处反查各有一条专用策略**（都只授 SELECT，且要求「无租户上下文 +
   对应的引导开关 on」）：
   - ``member`` 上的 ``member_login_lookup``：openid -> tenant_id（见
     ``app/services/auth.py``，开关 ``app.auth_bootstrap``）；
   - ``virtual_order`` 上的 ``xpay_deliver_lookup``：out_trade_no -> tenant_id
     （见 ``app/api/v1/entitlement.py`` 发货推送，开关 ``app.deliver_bootstrap``）。
   策略总数 = 32 条租户隔离 + 2 条引导 = **34**（见 ``config.EXPECTED_RLS_POLICIES``）。
5. 每张表都 ``FORCE ROW LEVEL SECURITY``：缺它时**表 owner 会绕过 RLS**，
   是最容易被忽略的漏洞。

Revision ID: 0001_initial
Revises: None
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

import app.models  # noqa: F401  - 导入即注册全部模型（34 张表）
from app.db.base import NULLABLE_TENANT_TABLES, PLATFORM_TABLES, Base
from app.tenant import rls

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def tenant_scoped_table_names() -> list[str]:
    """带 tenant_id 且不属于平台级白名单的表名（升序，保证渲染顺序可复现）。"""
    return sorted(
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.c and table.name not in PLATFORM_TABLES
    )


def create_tables() -> None:
    """建全部表（含索引、唯一约束、检查约束）。"""
    # MockConnection 与真实 Connection 都实现 _run_ddl_visitor，故离线/在线同一路径
    Base.metadata.create_all(bind=op.get_bind())


def create_rls() -> None:
    """在 PostgreSQL 上启用行级安全（ENABLE + FORCE + POLICY）；其他方言跳过。"""
    dialect_name = op.get_context().dialect.name
    if dialect_name != rls.PG_DIALECT:
        # 如实标注：非 PG 下没有 DB 级隔离，只有应用层 scope（不伪造证据）
        return

    for table_name in tenant_scoped_table_names():
        nullable_tenant = table_name in NULLABLE_TENANT_TABLES
        for statement in rls.rls_statements(table_name, nullable_tenant=nullable_tenant):
            op.execute(statement)

    # 登录引导：openid -> tenant_id 反查发生在租户上下文之前（RLS 鸡生蛋）
    op.execute(rls.member_bootstrap_policy_sql())
    # 发货引导：out_trade_no -> tenant_id 反查同理（虚拟支付推送没有 JWT）
    # 两条引导策略都只授 SELECT，且要求「无租户上下文 + 各自的引导开关 on」
    op.execute(rls.xpay_deliver_lookup_policy_sql())


def upgrade() -> None:
    """建 34 张表，并在 PG 上补齐 RLS 三件套。"""
    create_tables()
    create_rls()


def downgrade() -> None:
    """反序删表（策略随表一起消失）。"""
    Base.metadata.drop_all(bind=op.get_bind())
