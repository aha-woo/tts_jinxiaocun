"""0003 fix：RLS 策略里**空串 GUC 被当成 uuid** 会 500（真 PG 才暴露）。

问题（2026-11-17 在真 PostgreSQL 上跑 `test_rls_cross_tenant.py` 发现）
------------------------------------------------------------------
``set_config('app.tenant_id', ..., is_local=true)`` 是事务级设置。事务结束后这个
自定义 GUC **不会消失，而是变成空字符串** ``''``。连接池把同一条连接借给下一个
请求时，策略里的 ``current_setting('app.tenant_id', true)::uuid`` 会把 ``''``
当 uuid 解析，直接抛：

    invalid input syntax for type uuid: ""

后果比「看不见数据」严重：

1. 任何**没有租户上下文**的语句（``/healthz`` 的健康探针、迁移、运维脚本、
   登录时按 openid 反查）在复用连接上 **500**；
2. 两条引导策略用的是 ``... IS NULL``，空串不满足 ``IS NULL`` →
   **登录反查 / 发货反查失效**（用户会被当成新用户重新开通租户）。

修法
----
把三处表达式统一改成「空串当 NULL」：

    NULLIF(current_setting('app.tenant_id', true), '')::uuid
    NULLIF(current_setting('app.tenant_id', true), '') IS NULL

做法是 DROP + CREATE（策略不能 ALTER 表达式），共 34 条：
32 条租户隔离 + ``member_login_lookup`` + ``xpay_deliver_lookup``。

为什么必须单独一个迁移而不是改 0001
----------------------------------
已经跑过 0001 的库（含开发库）不会因为改 0001 而变好；迁移链必须能**升级存量**。
新库会依次执行 0001（旧表达式）→ 0003（覆盖成新表达式），最终状态一致。

Revision ID: 0003_fix_rls_empty_guc
Revises: 0002_seed
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

import app.models  # noqa: F401  - 导入即注册全部模型
from app.db.base import NULLABLE_TENANT_TABLES, PLATFORM_TABLES, Base
from app.tenant import rls

revision: str = "0003_fix_rls_empty_guc"
down_revision: str | None = "0002_seed"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def tenant_tables() -> list[str]:
    """需要租户隔离策略的表（带 tenant_id 且不在平台级白名单）。"""
    return sorted(
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.c and table.name not in PLATFORM_TABLES
    )


def _old_expression(table_name: str) -> str:
    """0001 的旧表达式（仅 downgrade 用，**不要**在生产复用）。"""
    if table_name in NULLABLE_TENANT_TABLES:
        return (
            f"(tenant_id IS NULL OR tenant_id = "
            f"current_setting('{rls.GUC_TENANT_ID}', true)::uuid)"
        )
    return f"(tenant_id = current_setting('{rls.GUC_TENANT_ID}', true)::uuid)"


def upgrade() -> None:
    """重建 34 条策略：空串 GUC 视为 NULL（PG only；SQLite 跳过且不假装）。"""
    if op.get_context().dialect.name != rls.PG_DIALECT:
        return

    for table_name in tenant_tables():
        op.execute(f"DROP POLICY IF EXISTS {rls.tenant_policy_name(table_name)} ON {table_name}")
        op.execute(
            rls.isolation_policy_sql(
                table_name, nullable_tenant=table_name in NULLABLE_TENANT_TABLES
            )
        )

    # 两条「鸡生蛋」引导策略：IS NULL 判空串不成立，必须一起修
    op.execute("DROP POLICY IF EXISTS member_login_lookup ON member")
    op.execute(rls.member_bootstrap_policy_sql())
    op.execute("DROP POLICY IF EXISTS xpay_deliver_lookup ON virtual_order")
    op.execute(rls.xpay_deliver_lookup_policy_sql())


def downgrade() -> None:
    """回退成 0001 的表达式（空串会 500 的那个版本）。"""
    if op.get_context().dialect.name != rls.PG_DIALECT:
        return

    for table_name in tenant_tables():
        op.execute(f"DROP POLICY IF EXISTS {rls.tenant_policy_name(table_name)} ON {table_name}")
        expression = _old_expression(table_name)
        op.execute(
            f"CREATE POLICY {rls.tenant_policy_name(table_name)} ON {table_name} "
            f"USING {expression} WITH CHECK {expression}"
        )

    op.execute("DROP POLICY IF EXISTS member_login_lookup ON member")
    op.execute(
        "CREATE POLICY member_login_lookup ON member FOR SELECT "
        f"USING (current_setting('{rls.GUC_TENANT_ID}', true) IS NULL "
        f"AND current_setting('{rls.GUC_AUTH_BOOTSTRAP}', true) = 'on')"
    )
    op.execute("DROP POLICY IF EXISTS xpay_deliver_lookup ON virtual_order")
    op.execute(
        "CREATE POLICY xpay_deliver_lookup ON virtual_order FOR SELECT "
        f"USING (current_setting('{rls.GUC_TENANT_ID}', true) IS NULL "
        f"AND current_setting('{rls.GUC_DELIVER_BOOTSTRAP}', true) = 'on')"
    )
