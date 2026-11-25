"""Engine / Session 工厂 + ORM 全局租户 scope（九道防线第 2 条）。

双后端
------
- **PostgreSQL（方案 A，生产）**：连接池 + pre-ping；事务内 `set_config('app.tenant_id', ..., true)`；
- **SQLite（本地/测试）**：`StaticPool` + `check_same_thread=False`（内存库跨连接共享），
  无 RLS —— `Database.capabilities()` 会如实返回 ``isolation_mode="app_scope_only"``。

ORM 全局 scope
--------------
`do_orm_execute` 事件给每条 SELECT 追加 `tenant_id = ctx.tenant_id` 的 loader criteria，
与 :class:`~app.tenant.repository.TenantRepository` 的显式 where 形成双保险。
登录引导（openid 反查）用 ``session.info[BYPASS_SCOPE_KEY] = True`` 显式豁免，
除此之外**任何代码都不应设置该标志**。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event, or_, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import with_loader_criteria

from app.db.base import Base, NullableTenantMixin, TenantMixin
from app.tenant import rls
from app.tenant.context import TenantContext

#: 会话级豁免标志（仅登录引导 / 迁移 / 运维脚本可使用）
BYPASS_SCOPE_KEY = "vil_bypass_tenant_scope"


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """按 URL 创建 Engine（自动适配 SQLite 内存库与 PG 连接池）。

    ⚠️ 已知局限（2026-11-19 云上踩到）：SQLite 默认**不校验外键**，而 SQLAlchemy 在
    没有 ``relationship()`` 时不保证按 FK 顺序发 INSERT。所以「先插子表、父表还没落库」
    这类错误在本地 SQLite 上永远绿，只有真 PG 才会暴露。

    曾尝试在 SQLite 连接上打开 ``PRAGMA foreign_keys=ON`` 来提前暴露，但现有 100+ 条
    测试夹具直接伪造 ``tenant_id`` 而不建 ``tenant`` 行，会被全部打断——那是一次独立的
    夹具清理，不在本次范围。**因此用 PG 专用回归测试兜底**：
    ``tests/test_provisioning_pg.py``（设置 ``PG_TEST_URL`` 时运行）。
    """
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {
            "echo": echo,
            "future": True,
            "connect_args": {"check_same_thread": False},
        }
        if ":memory:" in url:
            # 内存库必须共享同一连接，否则每个 session 看到不同的空库
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        # 修复 Python sqlite3 仅在 DML 前发 BEGIN、遇到 SAVEPOINT 不开外层事务导致
        # RELEASE SAVEPOINT 隐式提交的问题：仅在执行 SAVEPOINT 前若尚未在事务中则先 BEGIN。
        @event.listens_for(engine, "before_cursor_execute")
        def _sqlite_begin_before_savepoint(
            conn: Any,
            cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SAVEPOINT "):
                raw_conn = getattr(conn.connection, "dbapi_connection", conn.connection)
                if raw_conn is not None and not getattr(raw_conn, "in_transaction", True):
                    cursor.execute("BEGIN")

        return engine
    return create_engine(
        url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,  # 外挂托管实例常会掐空闲连接，需核实（Q22）
        # 建连超时：**没有它，DB 不可达时 /readyz 与启动自检会挂几分钟**（Windows 实测
        # 对关闭端口 SYN 重试可达 90s+；psycopg 默认 connect_timeout=0 表示无限等）。
        # 5 秒足够内网/公网建连，也让「连不上」快速变成 503 而不是把探针拖死。
        connect_args={"connect_timeout": 5},
        pool_timeout=10,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建 Session 工厂（关闭 autoflush 之外的隐式行为，避免意外写）。"""
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_scope(execute_state: Any) -> None:  # noqa: ANN401 - SQLAlchemy 事件对象
    """ORM 全局 scope：SELECT 自动追加 tenant_id 过滤（含可空租户表的平台行）。"""
    if not execute_state.is_select:
        return
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    if execute_state.session.info.get(BYPASS_SCOPE_KEY):
        return

    from app.models.inventory import InventoryPolicy  # 局部导入：避免循环依赖
    from app.tenant.context import get as get_context  # 局部导入：避免循环依赖

    context = get_context()
    if context is None:
        # 无上下文时不注入（由 TenantRepository 负责报错）；健康检查等只读元数据查询不受影响
        return

    tenant_id = context.tenant_id
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        ),
        with_loader_criteria(
            InventoryPolicy,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        ),
        with_loader_criteria(
            NullableTenantMixin,
            lambda cls: or_(cls.tenant_id.is_(None), cls.tenant_id == tenant_id),
            include_aliases=True,
        ),
    )


@dataclass
class Database:
    """Engine + Session 工厂 + 隔离能力自描述。"""

    engine: Engine
    session_factory: sessionmaker[Session]

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> "Database":
        """按 URL 构造。"""
        engine = create_db_engine(url, echo=echo)
        return cls(engine=engine, session_factory=create_session_factory(engine))

    @property
    def dialect_name(self) -> str:
        """方言名（postgresql / sqlite …）。"""
        return self.engine.dialect.name

    @property
    def is_pg(self) -> bool:
        """是否 PostgreSQL（方案 A）。"""
        return self.dialect_name == rls.PG_DIALECT

    def capabilities(self) -> dict[str, Any]:
        """隔离能力自描述（供 `/readyz` 与运维手册如实标注）。

        ``tables`` 是**库里实际存在的业务表数**（不含 ``alembic_version``）——
        就绪探针关心的是「迁移到底跑了没有」，不是「模型声明了多少张表」；
        模型声明数另放在 ``tables_declared``。
        """
        info = rls.capabilities(self.engine)
        declared = len(Base.metadata.tables)
        try:
            actual = len([name for name in self.table_names() if name != "alembic_version"])
        except Exception:  # noqa: BLE001 - 库连不上时不让探针自己崩掉
            actual = 0
        info["tables"] = actual
        info["tables_declared"] = declared
        info["has_forbidden_bypass_flag"] = BYPASS_SCOPE_KEY
        return info

    @contextmanager
    def session(
        self,
        context: TenantContext | None = None,
        *,
        bypass_scope: bool = False,
    ) -> Iterator[Session]:
        """开一个事务性 Session：注入 RLS GUC（PG）并在异常时回滚。

        传入 ``context`` 时会**同时**把它绑定到 contextvar（离开时复位）：
        这样 ``TenantRepository`` 与 ORM 全局 scope 在同一个会话里看到的一定是同一个租户，
        避免「GUC 是 A、contextvar 是 B（或干脆缺失）」这种最难查的错位。

        幂等绑定（``bound == context`` 时不再二次 bind）
        ------------------------------------------------
        ``get_tenant_session`` 是**同步生成器依赖**，Starlette 会把它交给
        ``contextmanager_in_threadpool`` 执行：``__enter__`` 与 ``__exit__``
        各自跑在不同线程、且各自持有**当前 context 的副本**。于是
        「进线程时 ``bind()`` 拿 token」+「出线程时 ``reset(token)``」必然跨 context，
        token 在另一个副本里无效 —— 这正是 ``context_reset_cross_context_total``
        每请求 +1 的来源（不是数据错位，但会让这个「不变式计数器」失去意义）。

        请求链路里 ``require_auth`` 已经在本请求 task 里绑定过**同一个**上下文，
        此时再 bind 一次纯属重复，故：上下文已存在且相同 → 只设 GUC、不 bind。
        """
        from app.tenant.context import bind, get as get_context, reset  # 局部导入：避免循环依赖

        session = self.session_factory()
        token = None
        try:
            if bypass_scope:
                session.info[BYPASS_SCOPE_KEY] = True
            elif context is not None:
                rls.set_tenant_guc(session, context.tenant_id)
                already = get_context()
                if already is None or already != context:
                    token = bind(context)
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if token is not None:
                reset(token)
            session.close()

    def create_all(self) -> None:
        """建全部表（仅测试/本地；生产走 Alembic）。"""
        Base.metadata.create_all(bind=self.engine)

    def drop_all(self) -> None:
        """删全部表（仅测试）。"""
        Base.metadata.drop_all(bind=self.engine)

    def table_names(self) -> list[str]:
        """已建表名（升序，测试断言用）。"""
        inspector_url = self.engine
        from sqlalchemy import inspect  # 局部导入：避免顶层再引一个符号

        return sorted(inspect(inspector_url).get_table_names())

    def ping(self) -> bool:
        """连通性探测（`/readyz` 用）。"""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 探测失败只回 False，不抛
            return False

    def dispose(self) -> None:
        """释放连接池。"""
        self.engine.dispose()


def bind_tenant_to_session(session: Session, tenant_id: uuid.UUID | str) -> bool:
    """把租户 GUC 绑到已有会话（Repository 之外的手工事务用）。"""
    return rls.set_tenant_guc(session, tenant_id)
