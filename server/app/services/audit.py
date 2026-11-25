"""审计留痕：**所有写操作都要留一条**（九道防线第 9 条）。

为什么审计不能省
---------------
小商户系统出问题时，唯一能回答「这 2 箱可乐是谁、什么时候、按哪句话记的」
就是这张表。审计漏一条，那天所有的账都说不清。

约定：

- ``action`` 用 ``<域>.<动作>``（``doc.confirm`` / ``doc.reverse`` / ``export.create``）；
- ``entity`` + ``entity_id`` 指向被改的对象；
- ``detail`` 放**可复核的结构化信息**（不含音频、不含敏感明文）；
- 审计**只增不改不删**——本模块不提供 update/delete。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ops import AuditLog

__all__ = ["AUDIT_ACTIONS", "AuditService"]

#: 已知动作白名单（防止手滑写出 ``doc.sav`` 这种查不到的动作名）
AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "doc.confirm",
        "doc.reverse",
        "doc.correct",
        "doc.cancel",
        "count.apply",
        "card.create",
        "card.expire",
        "card.edit",
        "card.cancel",
        "correction.record",
        "dict.teach",
        "dict.delete",
        "product.unit_upgrade",
        "member.preferences",
        "task.create",
        "task.approve",
        "task.reject",
        "export.create",
        "export.done",
        "delivery_note.parse",
        "delivery_note.confirm",
        "entitlement.order",
        "entitlement.deliver",
        "account.deletion.request",
        "account.deletion.cancel",
    }
)


class AuditService:
    """审计写入与查询。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（与业务写同事务）。
        """
        self._session = session

    def record(
        self,
        *,
        tenant_id: uuid.UUID,
        action: str,
        entity: str,
        entity_id: str | None = None,
        member_id: uuid.UUID | None = None,
        ip: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> AuditLog:
        """写一条审计（只 flush，不 commit）。

        Args:
            tenant_id: 租户。
            action: 动作（建议取 :data:`AUDIT_ACTIONS`）。
            entity: 实体名（``doc_header`` / ``confirm_card`` …）。
            entity_id: 实体 id。
            member_id: 操作人。
            ip: 来源 ip（可空）。
            detail: 结构化细节。

        Returns:
            已 flush 的 :class:`AuditLog`。

        Raises:
            ValueError: ``action`` 不在白名单（宁可报错也不写脏动作名）。
        """
        if action not in AUDIT_ACTIONS:
            raise ValueError(f"未登记的审计动作 {action!r}，请先补 AUDIT_ACTIONS")
        entry = AuditLog(
            tenant_id=tenant_id,
            member_id=member_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            ip=ip,
            detail=dict(detail or {}),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def history(
        self,
        *,
        tenant_id: uuid.UUID,
        entity: str | None = None,
        entity_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        """查审计（按实体/动作过滤）。"""
        statement = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
        if entity is not None:
            statement = statement.where(AuditLog.entity == entity)
        if entity_id is not None:
            statement = statement.where(AuditLog.entity_id == str(entity_id))
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        statement = statement.order_by(AuditLog.created_at.desc()).limit(max(1, limit))
        return list(self._session.execute(statement).scalars())

    def export(self, *, tenant_id: uuid.UUID, limit: int = 1000) -> list[dict[str, Any]]:
        """导出审计（**可查可导出**：合规要求，不做网页下载）。"""
        rows = self.history(tenant_id=tenant_id, limit=limit)
        return [
            {
                "log_id": str(row.log_id),
                "tenant_id": str(row.tenant_id),
                "member_id": None if row.member_id is None else str(row.member_id),
                "action": row.action,
                "entity": row.entity,
                "entity_id": row.entity_id,
                "ip": row.ip,
                "detail": dict(row.detail or {}),
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]


def _iso(value: datetime | None) -> str | None:
    """时间序列化（UTC ISO）。"""
    return value.isoformat() if value is not None else None
