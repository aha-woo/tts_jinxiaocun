"""方案 A 专属：PostgreSQL RLS 落地（`SET LOCAL` 事务级租户变量 + 能力自描述）。

**如实声明（架构 §4.2 第 1 条 / §4.5 / T02 验收 ⑤⑥）**
------------------------------------------------------
- PostgreSQL（方案 A）：DB 层 RLS 强制生效，是九道防线里的第 1 道；
- SQLite / 云开发数据库（方案 B 或本地测试）：**没有 RLS**。本模块在非 PG 方言下
  **显式无操作且不报错**，同时 `capabilities()["rls_enabled"] == False`、`isolation_mode`
  返回 `app_scope_only`，供 `/readyz`、README、运维手册如实标注
  「隔离强度下降：只剩应用层 TenantRepository + 唯一索引 tenant_id 打头 + CI 扫描 + 越权单测」。
- **绝不假装 SQLite 有 RLS**：任何代码路径都不允许在非 PG 下声称 DB 强制隔离。

为什么用 `set_config(..., is_local=true)` 而不是 `SET LOCAL`：PostgreSQL 的 `SET` 不接受
绑定参数，`set_config()` 可以，且 `is_local=true` 等价于事务级设置——事务/连接归还池后自动复位，
不会把上一个请求的 tenant_id 泄漏给下一个请求。
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 租户上下文 GUC 名（RLS 策略表达式必须引用它）
GUC_TENANT_ID: Final[str] = "app.tenant_id"

#: 登录引导标志 GUC（仅用于「openid -> tenant_id」这一步的反查，见 migration 中的 member 策略）
GUC_AUTH_BOOTSTRAP: Final[str] = "app.auth_bootstrap"

#: 方案 A 的方言名
PG_DIALECT: Final[str] = "postgresql"


def dialect_name(bind: Any) -> str:  # noqa: ANN401 - Session/Connection/Engine 皆有 dialect
    """尽力取出方言名（Session / Connection / Engine 均可）。"""
    dialect = getattr(bind, "dialect", None)
    if dialect is not None:
        return str(dialect.name)
    get_bind = getattr(bind, "get_bind", None)
    if callable(get_bind):
        try:
            return str(get_bind().dialect.name)
        except Exception:  # noqa: BLE001 - 无绑定信息时按非 PG 处理（保守）
            return "unknown"
    return "unknown"


def is_pg(bind: Any) -> bool:  # noqa: ANN401
    """当前绑定是否为 PostgreSQL。"""
    return dialect_name(bind) == PG_DIALECT


def set_tenant_guc(session: Session, tenant_id: uuid.UUID | str) -> bool:
    """在**当前事务**内设置 `app.tenant_id`（RLS 策略据此过滤）。

    Args:
        session: 已开启（或即将开启）事务的会话。
        tenant_id: 租户 id。

    Returns:
        True = 已在 PG 上设置；False = 非 PG 方言下无操作（应用层隔离模式）。
    """
    if not is_pg(session):
        return False
    session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": GUC_TENANT_ID, "value": str(tenant_id)},
    )
    return True


def set_auth_bootstrap(session: Session, enabled: bool = True) -> bool:
    """开启/关闭登录引导标志（只允许在 PG 上生效）。

    该标志只放开 `member` 表的**只读**反查（见迁移中的 `member_login_lookup` 策略），
    且必须与「精确 openid 索引查询 + 登录限流」一起使用；不得用于业务查询。
    """
    if not is_pg(session):
        return False
    session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": GUC_AUTH_BOOTSTRAP, "value": "on" if enabled else "off"},
    )
    return True


def capabilities(bind: Any) -> dict[str, Any]:  # noqa: ANN401
    """隔离能力自描述（`/readyz` 与运维手册的单一来源）。

    Returns:
        含 ``dialect`` / ``rls_enabled`` / ``isolation_mode`` / ``notes`` 的字典。
    """
    name = dialect_name(bind)
    pg = name == PG_DIALECT
    return {
        "dialect": name,
        "rls_enabled": pg,
        "isolation_mode": "db_rls" if pg else "app_scope_only",
        "notes": (
            "方案 A：DB 层 RLS 强制（ENABLE + FORCE + tenant_isolation 策略，应用账号非 BYPASSRLS）"
            if pg
            else "非 PG 方言：无 RLS，隔离仅靠 TenantRepository 强制 scope + 唯一索引 tenant_id 打头 + "
            "CI 静态扫描 + 双租户越权单测；隔离强度明确低于方案 A，不得声称 RLS 仍在"
        ),
    }


def tenant_policy_name(table_name: str) -> str:
    """租户隔离策略名（迁移与测试共用）。"""
    return f"{table_name}_tenant_isolation"


def tenant_policy_expressions(table_name: str, *, nullable_tenant: bool) -> tuple[str, str]:
    """返回 (USING 表达式, WITH CHECK 表达式)。

    可空租户表（`template_rule` / `template_candidate` / `num_normalization_rules`）
    必须容忍平台层行（``tenant_id IS NULL``），否则平台模板会被 RLS 挡掉。

    为什么是 ``current_setting('app.tenant_id', true)``（第二个参数 = missing_ok）：
    不带 ``true`` 时，**未设置该 GUC 的连接**执行任何查询都会抛
    ``unrecognized configuration parameter``（迁移、运维脚本、登录引导阶段都会踩到）。
    带 ``true`` 时返回 NULL，比较结果恒为 NULL（视为 false），语义是
    **「没有租户上下文 = 一行都看不见」的安全拒绝**，而不是把整个库打挂。
    """
    if nullable_tenant:
        expression = (
            f"(tenant_id IS NULL OR tenant_id = current_setting('{GUC_TENANT_ID}', true)::uuid)"
        )
        return expression, expression
    expression = f"(tenant_id = current_setting('{GUC_TENANT_ID}', true)::uuid)"
    return expression, expression


def rls_statements(table_name: str, *, nullable_tenant: bool = False) -> list[str]:
    """生成某张表的 RLS 三件套（ENABLE / FORCE / POLICY）DDL。

    只应在 PG 迁移中执行；SQLite 不支持这些语句。
    """
    using, check = tenant_policy_expressions(table_name, nullable_tenant=nullable_tenant)
    return [
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        # 缺 FORCE 时表 owner 会绕过 RLS —— 这条不能漏
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {tenant_policy_name(table_name)} ON {table_name} "
        f"USING {using} WITH CHECK {check}",
    ]


def member_bootstrap_policy_sql() -> str:
    """登录引导策略：仅在 `app.tenant_id` 未设置且 `app.auth_bootstrap=on` 时放开 member 只读。

    说明：`wx.login` 的 openid -> tenant_id 反查发生在租户上下文建立**之前**，
    属于 RLS 的鸡生蛋问题。这里用「会话级显式开关」而不是放开整表读取：
    必须同时满足「无租户上下文」+「登录引导标志」两个条件，且只授 SELECT；
    业务查询绝不允许打开这个标志（见 ``app/services/auth.py`` 的登录链路）。
    """
    return (
        "CREATE POLICY member_login_lookup ON member FOR SELECT "
        f"USING (current_setting('{GUC_TENANT_ID}', true) IS NULL "
        f"AND current_setting('{GUC_AUTH_BOOTSTRAP}', true) = 'on')"
    )
