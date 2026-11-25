"""统一待处理条目服务（``gap`` 表读写）。"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ops import Gap
from app.utils.date_utils import now_utc


def upsert_gap(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    kind: str,
    ref_entity: str,
    ref_id: uuid.UUID,
    title: str,
    detail_json: Mapping[str, Any] | None = None,
    priority: int = 50,
) -> Gap:
    """新增或刷新同目标的未解决待处理（幂等，不重复刷屏）。"""
    existing = session.execute(
        select(Gap)
        .where(
            Gap.tenant_id == tenant_id,
            Gap.store_id == store_id,
            Gap.kind == kind,
            Gap.ref_entity == ref_entity,
            Gap.ref_id == ref_id,
            Gap.status != "resolved",
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.title = title
        existing.detail_json = dict(detail_json) if detail_json is not None else None
        existing.priority = priority
        session.flush()
        return existing

    gap = Gap(
        tenant_id=tenant_id,
        store_id=store_id,
        kind=kind,
        ref_entity=ref_entity,
        ref_id=ref_id,
        title=title,
        detail_json=dict(detail_json) if detail_json is not None else None,
        priority=priority,
        status="open",
    )
    session.add(gap)
    session.flush()
    return gap


def resolve_gaps(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID | None = None,
    kind: str,
    ref_entity: str,
    ref_id: uuid.UUID,
) -> int:
    """将匹配对象的未解决待处理标为已解决。"""
    statement = select(Gap).where(
        Gap.tenant_id == tenant_id,
        Gap.kind == kind,
        Gap.ref_entity == ref_entity,
        Gap.ref_id == ref_id,
        Gap.status != "resolved",
    )
    if store_id is not None:
        statement = statement.where(Gap.store_id == store_id)
    rows = list(session.execute(statement).scalars())
    now = now_utc()
    for row in rows:
        row.status = "resolved"
        row.resolved_at = now
    if rows:
        session.flush()
    return len(rows)
