"""导出任务（R3）：异步 + 强制租户隔离 + 对象存储短期签名 URL + **不做网页下载**。

四道隔离（漏一条就是数据事故）
------------------------
1. **强制 ``tenant_id``**：所有查询（含子查询与 JOIN）都必须显式带租户条件；
   本模块提供 :func:`assert_tenant_scoped` 在跑 SQL 前做静态检查；
2. **对象存储路径 ``t/{tenant_id}/`` 前缀**：即使签名 URL 泄漏，路径本身也带租户，
   越权访问会落到别的租户目录（对象存储策略按前缀鉴权）；
3. **短 TTL 签名 URL**：默认 5 分钟【假设】，过期即失效；
4. **交付方式**：文件生成后经微信**转发 / 保存 / 预览**，**不提供网页下载**
   （本项目无网页端，也绝不生成对外 H5 链接）。

另外：导出内容**不含音频**（``voice_log`` 只存 ``asr_text``，音频默认不留存），
也不含跨租户数据；每次导出写 ``audit_log``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.ops import ExportJob
from app.services.audit import AuditService

__all__ = [
    "DEFAULT_SIGNED_URL_TTL_SECONDS",
    "EXPORT_SCOPES",
    "FORBIDDEN_COLUMNS",
    "ExportOutcome",
    "ExportService",
    "assert_tenant_scoped",
    "object_key",
]

#: 可导出的范围（与 PRD §6 报表口径对应）
EXPORT_SCOPES: tuple[str, ...] = ("docs", "stock", "debt", "audit", "usage")

#: 签名 URL 默认 TTL（秒）【假设，需核实】
DEFAULT_SIGNED_URL_TTL_SECONDS = 300

#: **绝不允许**出现在导出数据里的列（音频与容器内部字段）
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "audio_object_ref",
        "audio_bytes",
        "audio_base64",
        "asr_audio",
        "raw_audio",
        "file_body",
        "password",
        "phone_plain",
    }
)


def object_key(tenant_id: str, job_id: str, file_format: str) -> str:
    """对象存储 key：**必须** ``t/{tenant_id}/`` 打头。"""
    return f"t/{tenant_id}/exports/{job_id}.{file_format}"


def assert_tenant_scoped(statement_text: str) -> None:
    """静态检查 SQL 是否带租户条件（导出这类批量读的兜底）。

    Args:
        statement_text: 归一化后的 SQL 片段。

    Raises:
        AppError: 未带 ``tenant_id`` 条件。
    """
    lowered = " ".join(statement_text.lower().split())
    if "tenant_id" not in lowered:
        raise AppError(
            "E_TENANT_DENIED",
            message="导出查询未带 tenant_id 条件，拒绝执行（导出是跨表批量读，最怕漏租户）",
            data={"rule": "export_must_be_tenant_scoped"},
        )


@dataclass(frozen=True)
class ExportOutcome:
    """一次导出任务的结果。"""

    job_id: str
    status: str
    scope: str
    file_format: str
    file_ref: str | None = None
    signed_url: str | None = None
    signed_url_expires_at: datetime | None = None
    idempotent: bool = False
    row_count: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态（**明确标注不可网页下载**）。"""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "scope": self.scope,
            "file_format": self.file_format,
            "file_ref": self.file_ref,
            "signed_url": self.signed_url,
            "signed_url_expires_at": (
                self.signed_url_expires_at.isoformat() if self.signed_url_expires_at else None
            ),
            "idempotent": self.idempotent,
            "row_count": self.row_count,
            "delivery": "经微信转发 / 保存 / 预览交付，不提供网页下载",
            "detail": self.detail,
        }


class ExportService:
    """导出任务服务（R3：只在电脑版微信大屏发起）。"""

    def __init__(
        self,
        session: Session,
        *,
        signed_url_ttl_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
            signed_url_ttl_seconds: 签名 URL 有效期。
        """
        self._session = session
        self._ttl = int(signed_url_ttl_seconds)
        self._audit = AuditService(session)

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        scope: str,
        file_format: str = "csv",
        period: str | None = None,
        idempotency_key: str,
    ) -> ExportOutcome:
        """创建导出任务（**幂等**：同一幂等键只建一个任务）。

        Args:
            tenant_id: 租户。
            member_id: 发起人（必须是 boss，由 API 层判）。
            scope: 导出范围。
            file_format: ``csv`` / ``xlsx``。
            period: 区间（``YYYY-MM`` 或 ``YYYY-MM-DD~YYYY-MM-DD``）。
            idempotency_key: 幂等键。

        Returns:
            :class:`ExportOutcome`。
        """
        if scope not in EXPORT_SCOPES:
            raise AppError(
                "E_VALIDATION",
                message=f"不支持的导出范围 {scope!r}",
                data={"allowed": list(EXPORT_SCOPES)},
            )
        if file_format not in {"csv", "xlsx"}:
            raise AppError("E_VALIDATION", message=f"不支持的导出格式 {file_format!r}")

        existing = self._find_by_key(tenant_id, idempotency_key)
        if existing is not None:
            return self._outcome_of(existing, idempotent=True, detail="幂等返回已有导出任务")

        job = ExportJob(
            tenant_id=tenant_id,
            scope=scope,
            file_format=file_format,
            period=period,
            status="queued",
            idempotency_key=idempotency_key,
        )
        self._session.add(job)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            existing = self._find_by_key(tenant_id, idempotency_key)
            if existing is None:  # pragma: no cover
                raise
            return self._outcome_of(existing, idempotent=True, detail="并发重复请求，幂等返回")

        self._audit.record(
            tenant_id=tenant_id,
            action="export.create",
            entity="export_job",
            entity_id=str(job.job_id),
            member_id=member_id,
            detail={"scope": scope, "format": file_format, "period": period},
        )
        return self._outcome_of(job, detail="导出任务已排队（异步生成，经微信转发交付）")

    # ------------------------------------------------------------------ 完成
    def complete(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        row_count: int = 0,
        member_id: uuid.UUID | None = None,
    ) -> ExportOutcome:
        """标记任务完成并给出短期签名 URL（**本地实现，真实对象存储需核实**）。"""
        job = self._get(tenant_id, job_id)
        if job is None:
            raise AppError("E_VALIDATION", message="导出任务不存在或不属于当前租户")
        key = object_key(str(tenant_id), str(job_id), str(job.file_format))
        job.file_ref = key
        job.status = "done"
        self._session.flush()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        self._audit.record(
            tenant_id=tenant_id,
            action="export.done",
            entity="export_job",
            entity_id=str(job.job_id),
            member_id=member_id,
            detail={"file_ref": key, "row_count": row_count},
        )
        outcome = self._outcome_of(job, row_count=row_count)
        return ExportOutcome(
            job_id=outcome.job_id,
            status=outcome.status,
            scope=outcome.scope,
            file_format=outcome.file_format,
            file_ref=key,
            signed_url=f"local-signed://{key}?ttl={self._ttl}",
            signed_url_expires_at=expires_at,
            row_count=row_count,
            detail="生成完毕：经微信转发/保存/预览，不提供网页下载",
        )

    # ------------------------------------------------------------------ 查询
    def list_jobs(self, *, tenant_id: uuid.UUID, limit: int = 20) -> list[ExportOutcome]:
        """列出导出任务。"""
        statement = (
            select(ExportJob)
            .where(ExportJob.tenant_id == tenant_id)
            .order_by(ExportJob.created_at.desc())
            .limit(max(1, limit))
        )
        return [self._outcome_of(job) for job in self._session.execute(statement).scalars()]

    # ------------------------------------------------------------------ 数据安全
    @staticmethod
    def sanitize(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """剔除禁止字段（音频/明文手机号），并断言每行都带 ``tenant_id``。

        Args:
            rows: 待导出数据行。

        Returns:
            清洗后的行列表。

        Raises:
            AppError: 出现跨租户行（没有 ``tenant_id`` 或全租户混合）。
        """
        cleaned: list[dict[str, Any]] = []
        tenant_ids: set[str] = set()
        for row in rows:
            if "tenant_id" not in row:
                raise AppError(
                    "E_TENANT_DENIED",
                    message="导出数据行缺少 tenant_id，拒绝导出（无法证明租户隔离）",
                )
            tenant_ids.add(str(row["tenant_id"]))
            cleaned.append(
                {
                    key: value
                    for key, value in row.items()
                    if key not in FORBIDDEN_COLUMNS
                }
            )
        if len(tenant_ids) > 1:
            raise AppError(
                "E_TENANT_DENIED",
                message="导出数据里出现多个租户，拒绝导出（跨租户泄漏）",
                data={"tenant_count": len(tenant_ids)},
            )
        return cleaned

    # ------------------------------------------------------------------ 内部
    def _get(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> ExportJob | None:
        """按 id 取任务。"""
        return self._session.execute(
            select(ExportJob)
            .where(ExportJob.tenant_id == tenant_id, ExportJob.job_id == job_id)
            .limit(1)
        ).scalar_one_or_none()

    def _find_by_key(self, tenant_id: uuid.UUID, key: str) -> ExportJob | None:
        """按幂等键取任务。"""
        return self._session.execute(
            select(ExportJob)
            .where(ExportJob.tenant_id == tenant_id, ExportJob.idempotency_key == key)
            .limit(1)
        ).scalar_one_or_none()

    def _outcome_of(
        self, job: ExportJob, *, idempotent: bool = False, row_count: int = 0, detail: str = ""
    ) -> ExportOutcome:
        """实体 → 结果。"""
        return ExportOutcome(
            job_id=str(job.job_id),
            status=str(job.status),
            scope=str(job.scope),
            file_format=str(job.file_format),
            file_ref=job.file_ref,
            idempotent=idempotent,
            row_count=row_count,
            detail=detail,
        )


def period_range(period: str | None) -> tuple[date, date]:
    """把 ``YYYY-MM`` / ``YYYY-MM-DD~YYYY-MM-DD`` 解析成日期区间。"""
    today = date.today()
    if not period:
        return today, today
    text_value = period.strip()
    if "~" in text_value:
        start_text, _, end_text = text_value.partition("~")
        return date.fromisoformat(start_text.strip()), date.fromisoformat(end_text.strip())
    if len(text_value) == 7:
        year, month = text_value.split("-")
        start = date(int(year), int(month), 1)
        end = (date(int(year) + (int(month) == 12), (int(month) % 12) + 1, 1)) - timedelta(days=1)
        return start, end
    single = date.fromisoformat(text_value)
    return single, single
