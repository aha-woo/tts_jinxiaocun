"""导出（R3）：强制租户 + 对象存储前缀 + 短 TTL + **不做网页下载**（验收⑧ 4.8）。

导出是唯一「一次读一大片数据」的接口，也是最容易出跨租户泄漏的地方。
四道隔离逐条验证：

1. **强制 ``tenant_id``**：查询不带租户条件直接拒（连子查询/JOIN 都要带）；
2. **对象存储路径 ``t/{tenant_id}/`` 前缀**：签名 URL 泄漏了也落不到别人的目录；
3. **短 TTL 签名 URL**：默认 5 分钟，过期即失效；
4. **交付方式**：经微信转发 / 保存 / 预览——本项目**没有网页端**，
   所以返回里绝不能出现「打开浏览器下载」这类引导。

另外：导出内容**不含音频**、**不含跨租户**，每次导出写 ``audit_log``。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.common.errors import AppError
from app.models.ops import AuditLog, ExportJob
from app.models.tenant import Tenant
from app.services.export_job import (
    DEFAULT_SIGNED_URL_TTL_SECONDS,
    EXPORT_SCOPES,
    FORBIDDEN_COLUMNS,
    ExportService,
    assert_tenant_scoped,
    object_key,
)
from t04_fixtures import new_database

TENANT_A = uuid.UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb")


@pytest.fixture()
def two_tenants():
    """两个租户的内存库（验证导出不会串租户）。"""
    database = new_database()
    with database.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_A, name="A 店"))
        session.add(Tenant(tenant_id=TENANT_B, name="B 店"))
    try:
        yield database
    finally:
        database.drop_all()
        database.dispose()


# ------------------------------------------------------------ ① 路径与文件
def test_object_key_is_always_tenant_prefixed() -> None:
    """对象存储 key 必须 ``t/{tenant_id}/`` 打头（前缀就是第二道鉴权）。"""
    job_id = str(uuid.uuid4())
    key = object_key(str(TENANT_A), job_id, "csv")
    assert key.startswith(f"t/{TENANT_A}/")
    assert key.endswith(".csv")
    assert str(TENANT_A) in key and str(TENANT_B) not in key


def test_export_jobs_never_cross_tenants(two_tenants) -> None:
    """A 的任务列表里不会出现 B 的任务；用 A 的租户去完成 B 的任务直接拒。"""
    with two_tenants.session(bypass_scope=True) as session:
        service = ExportService(session)
        job_a = service.create(
            tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="docs", idempotency_key="exp-a"
        )
    with two_tenants.session(bypass_scope=True) as session:
        service = ExportService(session)
        job_b = service.create(
            tenant_id=TENANT_B, member_id=uuid.uuid4(), scope="docs", idempotency_key="exp-a"
        )
    assert job_a.job_id != job_b.job_id, "跨租户同一幂等键撞了同一个任务"

    with two_tenants.session(bypass_scope=True) as session:
        service = ExportService(session)
        assert [job.job_id for job in service.list_jobs(tenant_id=TENANT_A)] == [job_a.job_id]
        assert [job.job_id for job in service.list_jobs(tenant_id=TENANT_B)] == [job_b.job_id]

    with pytest.raises(AppError) as excinfo:
        with two_tenants.session(bypass_scope=True) as session:
            ExportService(session).complete(
                tenant_id=TENANT_A, job_id=uuid.UUID(job_b.job_id), row_count=1
            )
    assert excinfo.value.resolved_code == "E_VALIDATION"
    assert "不属于当前租户" in excinfo.value.resolved_message


def test_signed_url_has_short_ttl_and_is_not_a_web_download(two_tenants) -> None:
    """签名 URL 短 TTL + 交付方式写死「经微信转发/保存/预览，不提供网页下载」。"""
    with two_tenants.session(bypass_scope=True) as session:
        service = ExportService(session)
        job = service.create(
            tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="stock", idempotency_key="exp-ttl"
        )
    with two_tenants.session(bypass_scope=True) as session:
        outcome = ExportService(session).complete(
            tenant_id=TENANT_A, job_id=uuid.UUID(job.job_id), row_count=12
        )

    assert DEFAULT_SIGNED_URL_TTL_SECONDS == 300
    assert outcome.signed_url is not None
    assert "http://" not in outcome.signed_url and "https://" not in outcome.signed_url, (
        "返回了网页下载链接：本项目没有网页端，文件只能经微信转发/保存/预览"
    )
    expires_at = outcome.signed_url_expires_at
    assert expires_at is not None
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(0) < delta <= timedelta(seconds=DEFAULT_SIGNED_URL_TTL_SECONDS + 5)
    payload = outcome.as_dict()
    assert "不提供网页下载" in payload["delivery"]
    assert payload["scope"] == "stock" and payload["row_count"] == 12


def test_export_writes_audit_log(two_tenants) -> None:
    """创建与完成各留一条审计（导出是可追责的动作）。"""
    with two_tenants.session(bypass_scope=True) as session:
        job = ExportService(session).create(
            tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="debt", idempotency_key="exp-audit"
        )
    with two_tenants.session(bypass_scope=True) as session:
        ExportService(session).complete(tenant_id=TENANT_A, job_id=uuid.UUID(job.job_id))
    with two_tenants.session(bypass_scope=True) as session:
        actions = [row.action for row in session.execute(select(AuditLog)).scalars()]
    assert "export.create" in actions and "export.done" in actions


# ------------------------------------------------------- ② SQL 必须带租户条件
def test_assert_tenant_scoped_rejects_unscoped_sql() -> None:
    """批量读的兜底：SQL 里没有 ``tenant_id`` 就拒（子查询/JOIN 也一样）。"""
    assert_tenant_scoped("SELECT * FROM doc_header WHERE tenant_id = :t")
    assert_tenant_scoped("SELECT * FROM doc_line l JOIN doc_header d ON l.doc_id = d.doc_id WHERE d.tenant_id = :t")
    with pytest.raises(AppError) as excinfo:
        assert_tenant_scoped("SELECT * FROM doc_header")
    assert excinfo.value.resolved_code == "E_TENANT_DENIED"


# ---------------------------------------------------------- ③ 内容清洗
def test_sanitize_strips_audio_and_forbidden_columns() -> None:
    """音频字段与明文手机号**必须**被剔除（且 ``FORBIDDEN_COLUMNS`` 里明确列出）。"""
    rows = [
        {
            "tenant_id": str(TENANT_A),
            "doc_no": "XS20260201001",
            "audio_object_ref": "t/a/audio/silk-1",
            "asr_text": "进了两箱可乐",
            "phone_plain": "13800000000",
            "phone_enc": "enc:xxx",
        }
    ]
    cleaned = ExportService.sanitize(rows)
    assert cleaned[0]["doc_no"] == "XS20260201001"
    assert cleaned[0]["asr_text"] == "进了两箱可乐", "导出行应保留文本（音频才是不留存的）"
    assert "audio_object_ref" not in cleaned[0]
    assert "phone_plain" not in cleaned[0]
    assert "phone_enc" in cleaned[0], "加密手机号是允许的（明文不行）"
    assert {"audio_object_ref", "audio_bytes", "phone_plain"}.issubset(FORBIDDEN_COLUMNS)


def test_sanitize_rejects_cross_tenant_rows() -> None:
    """一次导出里出现两个租户 → 直接拒（宁可失败，也不能泄漏）。"""
    with pytest.raises(AppError) as excinfo:
        ExportService.sanitize(
            [{"tenant_id": str(TENANT_A), "doc_no": "A1"}, {"tenant_id": str(TENANT_B), "doc_no": "B1"}]
        )
    assert excinfo.value.resolved_code == "E_TENANT_DENIED"
    assert excinfo.value.data["tenant_count"] == 2

    with pytest.raises(AppError):
        ExportService.sanitize([{"doc_no": "missing-tenant"}])  # 没有 tenant_id 无法证明隔离


# ------------------------------------------------------------- ④ 范围与幂等
def test_export_scope_whitelist_and_idempotency(two_tenants) -> None:
    """导出范围白名单（不含音频/跨租户），同一幂等键只建一个任务。"""
    assert set(EXPORT_SCOPES) == {"docs", "stock", "debt", "audit", "usage"}
    assert not any("audio" in scope or "voice" in scope for scope in EXPORT_SCOPES)

    with two_tenants.session(bypass_scope=True) as session:
        first = ExportService(session).create(
            tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="usage", idempotency_key="exp-dup"
        )
    with two_tenants.session(bypass_scope=True) as session:
        second = ExportService(session).create(
            tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="usage", idempotency_key="exp-dup"
        )
    assert second.job_id == first.job_id
    assert second.idempotent is True
    with two_tenants.session(bypass_scope=True) as session:
        assert len(session.execute(select(ExportJob)).scalars().all()) == 1

    with pytest.raises(AppError):
        with two_tenants.session(bypass_scope=True) as session:
            ExportService(session).create(
                tenant_id=TENANT_A, member_id=uuid.uuid4(), scope="audio", idempotency_key="exp-bad"
            )
