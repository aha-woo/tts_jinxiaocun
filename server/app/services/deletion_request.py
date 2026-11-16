"""销户申请（R3）：7 天冷静期 + 冷静期内**只读冻结** + 可撤销。

为什么要冷静期
-------------
销户是唯一「删了真的找不回来」的动作。冷静期内：

- **只读冻结**：所有写操作一律拒（``E_TENANT_DENIED``），但**已有数据仍可查**——
  老板要能导出、能核对，才敢确认；
- **可撤销**：7 天内随时撤销，撤销后立刻恢复正常；
- 冷静期结束才真正删（实际删数据是运维脚本 + 人工复核，不在本模块）。

销户**完全禁止语音**（R3 白名单），语音侧只会生成 ``pending_task``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.models.ops import DeletionRequest
from app.services.audit import AuditService

__all__ = [
    "COOL_OFF_DAYS",
    "DeletionOutcome",
    "DeletionService",
    "READONLY_FROZEN",
]

#: 冷静期天数【需核实合规】
COOL_OFF_DAYS = 7

#: 冻结标记（API 层据此拒绝写操作）
READONLY_FROZEN = "readonly_frozen"


@dataclass(frozen=True)
class DeletionOutcome:
    """一次销户申请/撤销的结果。"""

    request_id: str
    status: str
    cool_until: str | None = None
    frozen: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "request_id": self.request_id,
            "status": self.status,
            "cool_until": self.cool_until,
            "frozen": self.frozen,
            "cool_off_days": COOL_OFF_DAYS,
            "detail": self.detail,
        }


class DeletionService:
    """销户申请与撤销。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
        """
        self._session = session
        self._audit = AuditService(session)

    # ------------------------------------------------------------------ 申请
    def request(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> DeletionOutcome:
        """提交销户申请（进入冷静期，冻结写操作）。"""
        moment = now or datetime.now(timezone.utc)
        existing = self._active(tenant_id)
        if existing is not None:
            return DeletionOutcome(
                request_id=str(existing.request_id),
                status=str(existing.status),
                cool_until=existing.cool_until.isoformat() if existing.cool_until else None,
                frozen=True,
                detail="已在冷静期内（幂等返回）",
            )
        cool_until = (moment + timedelta(days=COOL_OFF_DAYS)).date()
        request = DeletionRequest(
            tenant_id=tenant_id,
            status="pending",
            cool_until=cool_until,
            reason=reason or "用户申请销户",
        )
        self._session.add(request)
        self._session.flush()
        self._audit.record(
            tenant_id=tenant_id,
            action="account.deletion.request",
            entity="deletion_request",
            entity_id=str(request.request_id),
            member_id=member_id,
            detail={"cool_until": cool_until.isoformat(), "cool_off_days": COOL_OFF_DAYS},
        )
        return DeletionOutcome(
            request_id=str(request.request_id),
            status="pending",
            cool_until=cool_until.isoformat(),
            frozen=True,
            detail=f"已进入 {COOL_OFF_DAYS} 天冷静期：冷静期内只读冻结，随时可撤销",
        )

    # ------------------------------------------------------------------ 撤销
    def cancel(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        request_id: uuid.UUID | None = None,
    ) -> DeletionOutcome:
        """撤销销户申请（立刻解除冻结）。"""
        request = (
            self._get(tenant_id, request_id) if request_id is not None else self._active(tenant_id)
        )
        if request is None:
            raise AppError("E_VALIDATION", message="没有可撤销的销户申请")
        if str(request.status) != "pending":
            return DeletionOutcome(
                request_id=str(request.request_id),
                status=str(request.status),
                frozen=False,
                detail="该申请已处理过，无需撤销",
            )
        request.status = "cancelled"
        self._session.flush()
        self._audit.record(
            tenant_id=tenant_id,
            action="account.deletion.cancel",
            entity="deletion_request",
            entity_id=str(request.request_id),
            member_id=member_id,
            detail={"restored": True},
        )
        return DeletionOutcome(
            request_id=str(request.request_id),
            status="cancelled",
            frozen=False,
            detail="已撤销销户，写操作恢复正常",
        )

    # ------------------------------------------------------------------ 状态
    def is_frozen(self, *, tenant_id: uuid.UUID) -> bool:
        """是否处于冷静期冻结态（API 中间件据此拒写）。"""
        return self._active(tenant_id) is not None

    def assert_writable(self, *, tenant_id: uuid.UUID) -> None:
        """写操作前置检查。

        Raises:
            AppError: ``E_TENANT_DENIED``(403) —— 冷静期内只读冻结。
        """
        if self.is_frozen(tenant_id=tenant_id):
            raise AppError(
                "E_TENANT_DENIED",
                message="账号处于销户冷静期：只能查看与导出，不能记账；可在 7 天内撤销",
                data={"state": READONLY_FROZEN},
            )

    def current(self, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        """当前销户状态（前端展示用）。"""
        request = self._active(tenant_id)
        if request is None:
            return {"status": "none", "frozen": False, "cool_off_days": COOL_OFF_DAYS}
        return {
            "request_id": str(request.request_id),
            "status": str(request.status),
            "cool_until": request.cool_until.isoformat() if request.cool_until else None,
            "frozen": True,
            "cool_off_days": COOL_OFF_DAYS,
        }

    # ------------------------------------------------------------------ 内部
    def _get(
        self, tenant_id: uuid.UUID, request_id: uuid.UUID | None
    ) -> DeletionRequest | None:
        """按 id 取申请。"""
        if request_id is None:
            return None
        return self._session.execute(
            select(DeletionRequest)
            .where(
                DeletionRequest.tenant_id == tenant_id,
                DeletionRequest.request_id == request_id,
            )
            .limit(1)
        ).scalar_one_or_none()

    def _active(self, tenant_id: uuid.UUID) -> DeletionRequest | None:
        """取当前 pending 的申请。"""
        return self._session.execute(
            select(DeletionRequest)
            .where(
                DeletionRequest.tenant_id == tenant_id,
                DeletionRequest.status == "pending",
            )
            .limit(1)
        ).scalar_one_or_none()


def cool_off_until(now: datetime | None = None) -> date:
    """冷静期截止日。"""
    return ((now or datetime.now(timezone.utc)) + timedelta(days=COOL_OFF_DAYS)).date()
