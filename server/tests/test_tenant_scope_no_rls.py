"""应用层租户隔离（SQLite 上**没有 RLS**，此处验证「少了 DB 那道防线」时的剩余防线）。

本文件回答一个问题：本地/方案 B（无 PostgreSQL RLS）时，围栏还剩几道？
答案是四道，本文件逐条验证：

1. ``TenantRepository`` 强制 scope（缺上下文即报错，绝不查全表）；
2. ORM 全局 scope（``do_orm_execute`` + ``with_loader_criteria``，连裸 ``select(Model)`` 都被过滤）；
3. 缓存 key 前缀 ``t:{tenant_id}:{store_id}:{biz}:{id}``（且可按租户批量清）；
4. 唯一索引 ``tenant_id`` 打头（跨租户同名不撞号，租户内重复必须被挡）。

同时**如实断言**：``capabilities()["rls_enabled"] is False``、``isolation_mode == "app_scope_only"``。
任何代码都不得在 SQLite 上声称 DB 级 RLS 生效。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401
from app.common.errors import AppError
from app.db.session import BYPASS_SCOPE_KEY, Database
from app.models.doc import DocHeader
from app.models.pay import GoodsItem
from app.tenant import guards, rls
from app.tenant.cache import MemoryCacheBackend, TenantCache, tenant_cache_key
from app.tenant.context import (
    TenantContext,
    TenantContextMissing,
    clear,
    is_bound,
    require,
    use_context,
)
from app.tenant.repository import PlatformRepository, TenantRepository, tenant_repository


@pytest.fixture()
def database() -> Iterator[Database]:
    """内存 SQLite 库（建全部 34 张表；**无 RLS**）。"""
    db = Database.from_url("sqlite+pysqlite:///:memory:")
    db.create_all()
    try:
        yield db
    finally:
        clear()
        db.drop_all()
        db.dispose()


def make_context(*, tenant_id: uuid.UUID | None = None) -> TenantContext:
    """造一个租户上下文（门店/员工 id 随机，仅用于隔离测试）。"""
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(),
        store_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role_code="boss",
        tenant_version=1,
    )


def make_doc(doc_no: str) -> DocHeader:
    """造一条单据（tenant_id / store_id 由仓储自动填充）。"""
    return DocHeader(
        doc_no=doc_no,
        intent_id="sale.create",
        doc_type="sale",
        biz_date=date(2026, 1, 1),
        created_by=uuid.uuid4(),
    )


def seed_two_tenants(database: Database, ctx_a: TenantContext, ctx_b: TenantContext) -> uuid.UUID:
    """租户 A 与 B 各写一条同名单据，返回 B 的单据 id。"""
    with database.session(context=ctx_a) as session:
        tenant_repository(session, DocHeader).add(make_doc("XSD-0001"))
    with database.session(context=ctx_b) as session:
        doc_b = make_doc("XSD-0001")  # 故意同名：跨租户不撞号
        tenant_repository(session, DocHeader).add(doc_b)
        session.flush()
        return doc_b.doc_id


# ------------------------------------------------------- 如实声明：SQLite 无 RLS


def test_capabilities_do_not_claim_rls_on_sqlite(database: Database) -> None:
    """SQLite 必须自曝「没有 RLS」，不许假装隔离强度与 PG 一致。"""
    capabilities = database.capabilities()
    assert capabilities["dialect"] == "sqlite"
    assert capabilities["rls_enabled"] is False
    assert capabilities["isolation_mode"] == "app_scope_only"
    from app.config import EXPECTED_TABLES

    assert capabilities["tables"] == EXPECTED_TABLES == 42
    assert "无 RLS" in capabilities["notes"]
    assert "隔离强度" in capabilities["notes"]


def test_set_tenant_guc_is_noop_without_pg(database: Database) -> None:
    """非 PG 方言下 set_tenant_guc 返回 False（明确「这次没设进去」）。"""
    with database.session() as session:
        assert rls.set_tenant_guc(session, uuid.uuid4()) is False
        assert rls.set_auth_bootstrap(session, True) is False


# ------------------------------------------------------------------ 第一道：仓储


def test_repository_refuses_to_run_without_context(database: Database) -> None:
    """缺租户上下文 → TenantContextMissing（映射 E_TENANT_DENIED），绝不查全表。"""
    assert is_bound() is False
    with database.session() as session:
        with pytest.raises(TenantContextMissing) as excinfo:
            tenant_repository(session, DocHeader)
        assert excinfo.value.error_code == "E_TENANT_DENIED"


def test_cross_tenant_reads_are_invisible(database: Database) -> None:
    """A 看不到 B 的单据：get 返回 None（不泄露存在性）、list/count 只算自己的。"""
    ctx_a, ctx_b = make_context(), make_context()
    doc_b_id = seed_two_tenants(database, ctx_a, ctx_b)

    with database.session(context=ctx_a) as session:
        repo_a = tenant_repository(session, DocHeader)
        assert repo_a.get(doc_b_id) is None, "跨租户按 id 读到了别人的数据"
        assert repo_a.count() == 1
        assert [row.doc_no for row in repo_a.list()] == ["XSD-0001"]
        with pytest.raises(AppError) as excinfo:
            repo_a.require(doc_b_id)
        assert excinfo.value.resolved_code == "E_NOT_FOUND"
        assert excinfo.value.resolved_status == 404


def test_cross_tenant_write_is_rejected(database: Database) -> None:
    """A 不能写 B 的数据：显式带别人 tenant_id 的新增被 E_TENANT_DENIED 拒绝。"""
    ctx_a, ctx_b = make_context(), make_context()
    with database.session(context=ctx_a) as session:
        repo_a = tenant_repository(session, DocHeader)
        forged = make_doc("XSD-9999")
        forged.tenant_id = ctx_b.tenant_id
        with pytest.raises(AppError) as excinfo:
            repo_a.add(forged)
        assert excinfo.value.resolved_code == "E_TENANT_DENIED"
        assert excinfo.value.resolved_status == 403


def test_update_rejects_tenant_id_change(database: Database) -> None:
    """禁止改 tenant_id（唯一「搬迁数据」的合法路径是销户导出 + 新建）。"""
    ctx_a = make_context()
    with database.session(context=ctx_a) as session:
        repo_a = tenant_repository(session, DocHeader)
        doc = repo_a.add(make_doc("XSD-0002"))
        session.flush()
        with pytest.raises(ValueError):
            repo_a.update(doc, tenant_id=uuid.uuid4())


def test_export_rows_contains_only_own_tenant(database: Database) -> None:
    """导出（T04 导出 Job 复用）：行集里不许出现其它租户的数据。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)
    with database.session(context=ctx_a) as session:
        rows = tenant_repository(session, DocHeader).export_rows()
    assert len(rows) == 1
    assert {str(row["tenant_id"]) for row in rows} == {str(ctx_a.tenant_id)}


# ------------------------------------------------------- 第二道：ORM 全局 scope


def test_orm_global_scope_filters_plain_select(database: Database) -> None:
    """即使绕过仓储写裸 ``select(DocHeader)``，全局 scope 也会注入 tenant_id 条件。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)

    with database.session(context=ctx_a) as session:
        with use_context(ctx_a):
            rows = session.scalars(select(DocHeader)).all()
        assert len(rows) == 1
        assert rows[0].tenant_id == ctx_a.tenant_id

    with database.session(context=ctx_b) as session:
        with use_context(ctx_b):
            rows = session.scalars(select(DocHeader)).all()
        assert len(rows) == 1
        assert rows[0].tenant_id == ctx_b.tenant_id


def test_orm_scope_is_off_when_no_context(database: Database) -> None:
    """无上下文时 scope 不注入（由仓储负责报错），健康检查类只读查询不受影响。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)
    with database.session() as session:
        assert len(session.scalars(select(DocHeader)).all()) == 2


def test_scope_bypass_flag_is_explicit_and_named(database: Database) -> None:
    """豁免必须显式打标（登录引导专用），且标志名固定、易被评审 grep 到。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)
    assert BYPASS_SCOPE_KEY == "vil_bypass_tenant_scope"

    with database.session(bypass_scope=True) as session:
        with use_context(ctx_a):
            # 打着豁免标志时才能跨租户看到两条（仅 auth 登录链路可用）
            assert len(session.scalars(select(DocHeader)).all()) == 2


# ------------------------------------------------------------ 第三道：缓存前缀


def test_cache_key_is_tenant_and_store_prefixed() -> None:
    """缓存 key 固定五段：t:{tenant_id}:{store_id}:{biz}:{id}。"""
    tenant_id, store_id = uuid.uuid4(), uuid.uuid4()
    key = tenant_cache_key(tenant_id, store_id, "confirm_card", "abc")
    assert key == f"t:{tenant_id}:{store_id}:confirm_card:abc"
    assert key.count(":") == 4
    assert tenant_cache_key(tenant_id, None, "confirm_card", "abc") == f"t:{tenant_id}:-:confirm_card:abc"


def test_cache_values_never_cross_tenants() -> None:
    """同一 (biz, id) 在两个租户下是两条独立缓存，且可按租户整体清理。"""
    backend = MemoryCacheBackend()
    cache = TenantCache(backend)
    ctx_a, ctx_b = make_context(), make_context()

    key_a = cache.set("confirm_card", "c1", {"v": "A"}, context=ctx_a)
    key_b = cache.set("confirm_card", "c1", {"v": "B"}, context=ctx_b)
    assert key_a != key_b
    assert cache.get("confirm_card", "c1", context=ctx_a) == {"v": "A"}
    assert cache.get("confirm_card", "c1", context=ctx_b) == {"v": "B"}

    assert cache.purge_tenant(ctx_a.tenant_id) == 1
    assert cache.get("confirm_card", "c1", context=ctx_a) is None
    assert cache.get("confirm_card", "c1", context=ctx_b) == {"v": "B"}


def test_cache_requires_context() -> None:
    """缺上下文不许产出「无前缀 key」（否则必然串数据）。"""
    cache = TenantCache(MemoryCacheBackend())
    with pytest.raises(TenantContextMissing):
        cache.set("confirm_card", "c1", {"v": "x"})


# ---------------------------------------------------- 第四道：唯一索引 tenant 打头


def test_unique_constraint_is_per_tenant_not_global(database: Database) -> None:
    """同名单据号：租户内重复必须被挡，跨租户互不影响。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)  # 两个租户都已各有 XSD-0001

    with pytest.raises(IntegrityError):
        with database.session(context=ctx_a) as session:
            tenant_repository(session, DocHeader).add(make_doc("XSD-0001"))
            session.flush()

    with database.session(context=ctx_a) as session:
        assert tenant_repository(session, DocHeader).add(make_doc("XSD-0002")) is not None


# ------------------------------------------------------------- contextvar 并发


async def test_contextvar_isolated_under_asyncio_gather(database: Database) -> None:
    """``asyncio.gather`` 下两个租户的上下文互不串味（contextvar 按 task 隔离）。"""
    ctx_a, ctx_b = make_context(), make_context()
    seed_two_tenants(database, ctx_a, ctx_b)

    async def worker(ctx: TenantContext) -> tuple[uuid.UUID, int, uuid.UUID]:
        with use_context(ctx):
            await asyncio.sleep(0)  # 强制让出，制造交错
            seen = require()
            with database.session(context=ctx) as session:
                await asyncio.sleep(0)
                rows = session.scalars(select(DocHeader)).all()
            return seen.tenant_id, len(rows), rows[0].tenant_id

    first, second = await asyncio.gather(worker(ctx_a), worker(ctx_b))
    assert first[0] == first[2] == ctx_a.tenant_id
    assert second[0] == second[2] == ctx_b.tenant_id
    assert first[1] == second[1] == 1


# --------------------------------------------------------- 平台级表必须走另一条路


def test_platform_tables_use_platform_repository(database: Database) -> None:
    """平台级表（道具/话术）不参与租户过滤；误用另一条路直接报错。"""
    with database.session() as session:
        session.add(
            GoodsItem(
                goods_id="quota_pack_500",
                goods_name="语音加油包（500 次）",
                goods_type="quota_pack",
                price_fen=500,
                quota_amount=500,
                validity_days=0,
                status="on_shelf",
            )
        )
        session.flush()
        platform = PlatformRepository(session, GoodsItem)
        assert [row.goods_id for row in platform.list()] == ["quota_pack_500"]

        with pytest.raises(ValueError):
            PlatformRepository(session, DocHeader)  # 业务表不许走平台仓储

    with database.session(context=make_context()) as session:
        with pytest.raises(ValueError):
            TenantRepository(session, GoodsItem)  # 平台表不许走租户仓储


# ------------------------------------------------------------------- 角色守卫


def test_readonly_role_cannot_write() -> None:
    """readonly 写业务数据 → E_ROLE_DENIED（403），与 OCR/语音链路无关。"""
    readonly = TenantContext(
        tenant_id=uuid.uuid4(),
        store_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role_code="readonly",
        tenant_version=1,
    )
    with pytest.raises(AppError) as excinfo:
        guards.require_writable(readonly)
    assert excinfo.value.resolved_code == "E_ROLE_DENIED"
    assert excinfo.value.resolved_status == 403


def test_r3_actions_are_boss_only() -> None:
    """R3 白名单动作只允许老板（且 T04 还要校验来源为电脑版微信大屏）。"""
    manager = TenantContext(
        tenant_id=uuid.uuid4(),
        store_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role_code="manager",
        tenant_version=1,
    )
    with pytest.raises(AppError):
        guards.require_r3(manager)


def test_frozen_tenant_is_denied() -> None:
    """冻结/销户中的租户拒绝写入（E_TENANT_DENIED）。"""
    ctx = make_context()
    with pytest.raises(AppError) as excinfo:
        guards.assert_tenant_active("frozen", ctx)
    assert excinfo.value.resolved_code == "E_TENANT_DENIED"
    guards.assert_tenant_active("active", ctx)  # active 不抛


# ------------------------------------------------- 第五道：CI 静态扫描（可执行验证）


def test_tenant_guard_scan_is_clean_and_covers_models() -> None:
    """CI 静态扫描必须 0 命中，且**确实扫到了** ``app/models/*``（否则等于没扫）。

    这条测试把「最后一道防线」也变成可执行断言：扫描脚本一旦被改坏（比如只扫 STRING、
    漏掉 f-string 拼出来的 SQL），或在模型层引入裸 SQL，``pytest -q`` 就会红。
    """
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "infra" / "ci" / "tenant-guard-scan.py"
    assert script.is_file(), "缺少 CI 扫描脚本"
    assert importlib.util.find_spec("tokenize") is not None

    completed = subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(repo_root),
        # 子进程的输出编码必须显式指定：父进程按 utf-8 解码，而中文 Windows 下
        # 子进程默认按 ANSI 代码页（cp936）输出 → 解码直接抛 UnicodeDecodeError，
        # 看起来像「扫描有命中」，其实是测试环境问题（Linux CI 上不会复现）。
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert completed.returncode == 0, f"扫描有命中：{completed.stdout}\n{completed.stderr}"
    report = json.loads(completed.stdout)
    assert report["hits"] == 0
    assert report["findings"] == []
    # 证明覆盖到模型层：14 个模型文件必须都被扫到
    assert report["coverage"]["server/app/models"] >= 14
    # T04 起：新增的 app/api 与 app/pay（按 id 批量读 / 无用户上下文的发货回调）
    # 也必须在覆盖率里**被点名**——「位于 server/app 下所以应该扫到了」不算证据
    assert report["coverage"]["server/app/api"] >= 10
    assert report["coverage"]["server/app/pay"] >= 3
    assert report["coverage"]["server/app/domain"] >= 5
    # f-string 拼出来的 RLS DDL 现在可见了，且被逐条结构豁免（不是「看不见所以没命中」）
    assert report["exemptions"] >= 5
