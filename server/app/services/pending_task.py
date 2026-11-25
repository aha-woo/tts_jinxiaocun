"""R3 待办：**语音只生成待办，最终执行只能在电脑版微信大屏管理页**（PRD 2.7 / H8）。

这道防线保护的是什么
------------------
R3 是「合并往来单位」「设置单位换算系数」「导出全部数据」「销户」这类
**一旦错了很难回滚**的动作。语音识别再准，也不该让它直接执行——
老板在嘈杂环境里说一句「把这两个客户合成一个」，听错一个字就是两家的账合并，
事后要靠人肉拆账。

所以：

1. 语音侧**只能** ``create`` 待办，任何路径不得直写业务表
   （``output_schema.ensure_not_direct_write`` 也会拦）；
2. 执行必须在**电脑版微信大屏管理页**：同一个 JWT + ``role=boss`` + 二次确认
   + 留痕，缺一项都不给执行 —— 手机端与语音端一律 ``E_R3_PC_REQUIRED``(1206)，
   角色不对 ``E_ROLE_DENIED``(1302)；
3. 本项目**没有网页端**，所以错误提示里绝不能引导到外部浏览器 / H5 / 客服转账。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.ops import PendingTask
from app.services.audit import AuditService

__all__ = [
    "BIG_SCREEN_DEVICE",
    "PendingTaskService",
    "TaskDecision",
    "TaskView",
]

#: 唯一允许执行 R3 的设备形态（电脑版微信大屏，``resizable`` 双栏）
BIG_SCREEN_DEVICE = "pc_bigscreen"

#: 唯一允许执行 R3 的角色
R3_ROLE = "boss"


@dataclass(frozen=True)
class TaskView:
    """待办视图（大屏审批箱里的一行）。"""

    task_id: str
    session_id: str
    intent_id: str
    payload: Mapping[str, Any]
    status: str
    created_at: str | None = None
    requires_pc: bool = True
    required_role: str = R3_ROLE

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "payload": dict(self.payload),
            "status": self.status,
            "created_at": self.created_at,
            "requires_pc": self.requires_pc,
            "required_role": self.required_role,
            "hint_zh": "该动作只能在电脑版微信大屏管理页确认（手机端与语音端不能执行）",
        }


@dataclass(frozen=True)
class TaskDecision:
    """审批结果。"""

    task_id: str
    status: str
    approved: bool
    executed: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "approved": self.approved,
            "executed": self.executed,
            "detail": self.detail,
        }


class PendingTaskService:
    """R3 待办的创建、列出与审批。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
        """
        self._session = session
        self._audit = AuditService(session)

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        intent_id: str,
        payload: Mapping[str, Any] | None = None,
        member_id: uuid.UUID | None = None,
    ) -> TaskView:
        """为 R3 动作生成待办（**幂等**：同一会话同一意图只生成一条）。

        Args:
            tenant_id: 租户。
            session_id: 语音会话 id。
            intent_id: R3 意图。
            payload: 待执行参数（解析结果，供大屏复核）。
            member_id: 发起人。

        Returns:
            :class:`TaskView`。
        """
        existing = self._find(tenant_id, session_id, intent_id)
        if existing is not None:
            return _view_of(existing)

        task = PendingTask(
            tenant_id=tenant_id,
            session_id=session_id,
            intent_id=intent_id,
            payload=dict(payload or {}),
            status="pending",
        )
        try:
            with self._session.begin_nested():
                self._session.add(task)
                self._session.flush()
        except IntegrityError:
            existing = self._find(tenant_id, session_id, intent_id)
            if existing is None:  # pragma: no cover - 唯一约束冲突后必然能查到
                raise
            return _view_of(existing)

        self._audit.record(
            tenant_id=tenant_id,
            action="task.create",
            entity="pending_task",
            entity_id=str(task.task_id),
            member_id=member_id,
            detail={"intent_id": intent_id, "session_id": str(session_id)},
        )
        return _view_of(task)

    # ------------------------------------------------------------------ 查询
    def list_pending(self, *, tenant_id: uuid.UUID, limit: int = 50) -> list[TaskView]:
        """待审批列表（大屏审批箱）。"""
        statement = (
            select(PendingTask)
            .where(PendingTask.tenant_id == tenant_id, PendingTask.status == "pending")
            .order_by(PendingTask.created_at.desc())
            .limit(max(1, limit))
        )
        return [_view_of(task) for task in self._session.execute(statement).scalars()]

    def get(self, *, tenant_id: uuid.UUID, task_id: uuid.UUID) -> TaskView | None:
        """按 id 取待办。"""
        task = self._session.execute(
            select(PendingTask)
            .where(PendingTask.tenant_id == tenant_id, PendingTask.task_id == task_id)
            .limit(1)
        ).scalar_one_or_none()
        return None if task is None else _view_of(task)

    # ------------------------------------------------------------------ 审批
    def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        approve: bool,
        role: str,
        device: str,
        second_confirm: bool,
        member_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> TaskDecision:
        """审批一条 R3 待办（**只有大屏 + boss + 二次确认才能过**）。

        Args:
            tenant_id: 租户。
            task_id: 待办 id。
            approve: 通过还是拒绝。
            role: 当前操作人角色。
            device: 当前设备形态（``pc_bigscreen`` 才允许）。
            second_confirm: 是否已完成二次确认。
            member_id: 操作人。
            now: 注入时间。

        Returns:
            :class:`TaskDecision`。

        Raises:
            AppError: ``E_ROLE_DENIED``(1302) / ``E_R3_PC_REQUIRED``(1206) /
                ``E_VALIDATION``(422，二次确认缺失或任务不存在)。
        """
        del now  # 当前不按时间窗限制审批，签名保留以便将来加「超时自动作废」
        task = self._session.execute(
            select(PendingTask)
            .where(PendingTask.tenant_id == tenant_id, PendingTask.task_id == task_id)
            .limit(1)
        ).scalar_one_or_none()
        if task is None:
            raise AppError("E_VALIDATION", message="待办不存在或不属于当前租户")

        # 顺序固定：先判角色（谁能做），再判设备（在哪做），最后判二次确认
        if (role or "").strip().lower() != R3_ROLE:
            raise AppError(
                "E_ROLE_DENIED",
                message=f"该动作需要 role={R3_ROLE}，当前角色 {role!r} 无权执行",
                data={"required_role": R3_ROLE},
            )
        if (device or "").strip().lower() != BIG_SCREEN_DEVICE:
            raise AppError(
                "E_R3_PC_REQUIRED",
                message="该动作只能在电脑版微信大屏管理页确认（手机端与语音端不能执行）",
                data={"required_device": BIG_SCREEN_DEVICE},
            )
        if not second_confirm:
            raise AppError("E_VALIDATION", message="R3 动作必须二次确认后才可执行")
        if task.status != "pending":
            return TaskDecision(
                task_id=str(task.task_id),
                status=str(task.status),
                approved=str(task.status) == "approved",
                executed=False,
                detail="该待办已处理过（幂等返回）",
            )

        task.status = "approved" if approve else "rejected"
        task.approved_by = member_id
        task.approved_at = datetime.now(timezone.utc)
        self._session.flush()
        self._audit.record(
            tenant_id=tenant_id,
            action="task.approve" if approve else "task.reject",
            entity="pending_task",
            entity_id=str(task.task_id),
            member_id=member_id,
            detail={"role": role, "device": device, "second_confirm": True},
        )
        return TaskDecision(
            task_id=str(task.task_id),
            status=str(task.status),
            approved=approve,
            # 真正的业务执行由大屏侧调用对应域服务；本服务只负责「准不准做」
            executed=False,
            detail="审批通过，执行由大屏管理页调用对应业务接口完成并留痕"
            if approve
            else "已驳回",
        )

    # ------------------------------------------------------------------ 内部
    def _find(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID, intent_id: str
    ) -> PendingTask | None:
        """按 (tenant, session, intent) 找待办（唯一约束对应）。"""
        statement = (
            select(PendingTask)
            .where(
                PendingTask.tenant_id == tenant_id,
                PendingTask.session_id == session_id,
                PendingTask.intent_id == intent_id,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()


def _view_of(task: PendingTask) -> TaskView:
    """实体 → 视图。"""
    return TaskView(
        task_id=str(task.task_id),
        session_id=str(task.session_id),
        intent_id=str(task.intent_id),
        payload=dict(task.payload or {}),
        status=str(task.status),
        created_at=task.created_at.isoformat() if task.created_at else None,
    )
