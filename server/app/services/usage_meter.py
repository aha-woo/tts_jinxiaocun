"""计次与流水：``usage_ledger`` 的**唯一写入口**（与卡片同事务）。

铁律（团队已拍，逐条对应用户的钱）
----------------------------------
1. **只有卡片成功生成才写流水**：``CARD_CREATED`` 是库里 ``CHECK`` 约束里**唯一的合法值**，
   想写别的值连数据库都过不去（``ck_usage_ledger_event_type``）——口头约定不如 DDL 硬；
2. **与卡片插入同一事务**：``flush`` 而不 ``commit``，由调用方（T04 ``confirm_card``）
   统一提交；任一失败整体回滚，不会出现「扣了次但没卡片」；
3. **幂等靠唯一约束**：``UNIQUE(tenant_id, session_id, event_type)``，
   重复确认同一会话 → ``E_DELIVER_DUPLICATE`` 不适用，这里返回 ``duplicated=True``
   且**不再加一次额度**；
4. **失败/取消/无意图/卡片超时一律不写、不扣**：:meth:`UsageMeter.refuse` 显式记录理由，
   绝不「先扣了再说」。

``used_count`` 的自增与流水插入在同一事务，两条数据永远同进同退。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.quota import UsageLedger, UsageQuota

__all__ = [
    "BILLABLE_EVENTS",
    "NON_BILLABLE_REASONS",
    "MeterResult",
    "UsageMeter",
    "current_period",
]

#: 唯一可写事件（与 ``ck_usage_ledger_event_type`` 保持一致）
BILLABLE_EVENTS: frozenset[str] = frozenset({"CARD_CREATED"})

#: 「不该扣次」的明确理由（**只用于返回与日志，绝不写库**）
NON_BILLABLE_REASONS: Mapping[str, str] = {
    "PARSE_FAILED": "解析失败（Schema 不合法/超时）",
    "CANCELLED": "用户取消（「别记了」）",
    "NO_INTENT": "没有识别出意图",
    "CARD_TIMEOUT": "卡片未确认即超时",
    "QUOTA_REJECTED": "额度不足被门禁拒绝",
    "GATE_REJECTED": "误触/时长达不到门槛",
    "NON_COMMAND": "非命令（闲聊/纯语气词）",
    "DUPLICATE_TEXT": "60s 内重复文本",
}


def current_period(now: datetime | None = None) -> str:
    """自然月归集口径 ``YYYY-MM``（与 ``usage_quota.period`` 一致）。"""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


@dataclass
class MeterResult:
    """一次计次尝试的结果。"""

    billed: bool
    reason: str
    duplicated: bool = False
    ledger_id: uuid.UUID | None = None
    used_count: int | None = None
    period: str = ""
    #: 说明文案（可直接进日志/审计）
    detail: str = ""
    created_card: bool = False

    @property
    def ok(self) -> bool:
        """是否处于「服务可继续」的状态（成功或幂等重复都算）。"""
        return self.billed or self.duplicated


class UsageMeter:
    """计次器：**唯一**写 ``usage_ledger`` 的地方。"""

    def __init__(
        self,
        session: Session,
        *,
        cost_estimate_fen: int | None = None,
        default_free_quota: int = 150,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（RLS 已生效）。
            cost_estimate_fen: 单次成本估算（分）【假设，需核实】。
            default_free_quota: 新月份行的免费额度（与 ``usage_quota`` 默认值一致）。
        """
        self._session = session
        self._cost_estimate_fen = cost_estimate_fen
        self._default_free_quota = default_free_quota

    # ------------------------------------------------------------------ 计次
    def bill_card(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        card: Any | None = None,
        event_type: str = "CARD_CREATED",
        period: str | None = None,
        cost_estimate_fen: int | None = None,
        now: datetime | None = None,
    ) -> MeterResult:
        """卡片生成成功 → 写流水 + 自增额度（**同一事务，只 flush**）。

        Args:
            tenant_id: 租户 id（显式传入，便于断言「按 tenant 打头」）。
            session_id: 语音会话 id（幂等键业务主体）。
            card: 已构造好的 ``ConfirmCard`` 实体（``None`` 表示调用方自己插入）。
            event_type: 只能是 ``CARD_CREATED``。
            period: ``YYYY-MM``；缺省取当前 UTC 月。
            cost_estimate_fen: 本次成本估算（分）。
            now: 注入当前时间（测试）。

        Returns:
            :class:`MeterResult`；``duplicated=True`` 表示该会话已计过次。

        Raises:
            ValueError: ``event_type`` 不是 ``CARD_CREATED``（防止误用本入口写别的流水）。
        """
        if event_type not in BILLABLE_EVENTS:
            raise ValueError(
                f"usage_ledger 只允许写 {sorted(BILLABLE_EVENTS)}；"
                f"{event_type!r} 属不该扣次的场景，请用 refuse() 而不是写库"
            )

        bucket = period or current_period(now)
        existing = self._find_ledger(tenant_id, session_id, event_type)
        if existing is not None:
            quota = self._get_quota(tenant_id, bucket)
            return MeterResult(
                billed=False,
                reason="duplicated",
                duplicated=True,
                ledger_id=existing,
                used_count=quota.used_count if quota is not None else None,
                period=bucket,
                detail="同一会话已计过次（唯一约束兜底），本次不重复扣",
            )

        if card is not None:
            self._session.add(card)
            self._session.flush()
        quota = self._get_or_create_quota(tenant_id, bucket)
        ledger = UsageLedger(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            cost_estimate_fen=cost_estimate_fen if cost_estimate_fen is not None else self._cost_estimate_fen,
        )
        self._session.add(ledger)
        quota.used_count = int(quota.used_count) + 1
        try:
            # **只 flush 不 commit**：与卡片插入同事务，由调用方统一提交
            self._session.flush()
        except IntegrityError:
            # 并发下唯一约束兜底：回滚本次加分后按「已计次」返回
            self._session.rollback()
            return MeterResult(
                billed=False,
                reason="duplicated",
                duplicated=True,
                period=bucket,
                detail="并发重复提交被唯一约束拦下，本次不重复扣",
            )

        return MeterResult(
            billed=True,
            reason="card_created",
            ledger_id=ledger.ledger_id,
            used_count=quota.used_count,
            period=bucket,
            created_card=card is not None,
            detail=f"{bucket} 已用 {quota.used_count}/{quota.free_quota}",
        )

    def refuse(
        self,
        reason: str,
        *,
        session_id: uuid.UUID | None = None,
        detail: str = "",
    ) -> MeterResult:
        """**明确不扣次**（解析失败/取消/无意图/超时/门禁拒绝）。

        本方法**不碰数据库**：只在返回值与日志里留痕。
        这就是「失败不扣次」在代码层的落实方式——不是靠调用方自觉，而是没有写入口。
        """
        if reason not in NON_BILLABLE_REASONS:
            raise ValueError(f"未知的不扣次理由 {reason!r}；请先在 NON_BILLABLE_REASONS 登记")
        text = NON_BILLABLE_REASONS[reason]
        return MeterResult(
            billed=False,
            reason=reason,
            detail=f"{text}{('：' + detail) if detail else ''}"
            + (f"（session={session_id}）" if session_id is not None else ""),
        )

    # ------------------------------------------------------------------ 查询
    def usage(self, tenant_id: uuid.UUID, *, period: str | None = None) -> Mapping[str, Any]:
        """查某租户当月用量（含降级档位）。"""
        bucket = period or current_period()
        quota = self._get_quota(tenant_id, bucket)
        if quota is None:
            return {
                "tenant_id": str(tenant_id),
                "period": bucket,
                "used_count": 0,
                "free_quota": self._default_free_quota,
                "remaining": self._default_free_quota,
                "degrade_level": "L0",
            }
        return {
            "tenant_id": str(tenant_id),
            "period": bucket,
            "used_count": int(quota.used_count),
            "free_quota": int(quota.free_quota),
            "remaining": max(0, int(quota.free_quota) - int(quota.used_count)),
            "degrade_level": quota.degrade_level,
        }

    def history(
        self,
        *,
        limit: int = 100,
        period: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """流水历史（**可查**；降级/对账排查用）。"""
        statement = select(UsageLedger).order_by(UsageLedger.created_at.desc()).limit(max(1, limit))
        rows = list(self._session.execute(statement).scalars())
        bucket = period
        export: list[Mapping[str, Any]] = []
        for row in rows:
            record = {
                "ledger_id": str(row.ledger_id),
                "tenant_id": str(row.tenant_id),
                "session_id": str(row.session_id),
                "event_type": row.event_type,
                "cost_estimate_fen": row.cost_estimate_fen,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            if bucket and not str(record["created_at"] or "").startswith(bucket):
                continue
            export.append(record)
        return tuple(export)

    def export(self, *, period: str | None = None, limit: int = 1000) -> tuple[Mapping[str, Any], ...]:
        """导出流水（**可导出**：CSV/JSON 由 T04 序列化，本层只给结构化行）。"""
        return self.history(limit=limit, period=period)

    def total_cost_fen(self, *, period: str | None = None, limit: int = 1000) -> int:
        """某期成本合计（分）。"""
        rows: Iterable[Mapping[str, Any]] = self.history(limit=limit, period=period)
        return sum(int(row.get("cost_estimate_fen") or 0) for row in rows)

    # ------------------------------------------------------------------ 内部
    def _find_ledger(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        event_type: str,
    ) -> uuid.UUID | None:
        """按幂等键找已有流水。"""
        statement = (
            select(UsageLedger.ledger_id)
            .where(
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.session_id == session_id,
                UsageLedger.event_type == event_type,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _get_quota(self, tenant_id: uuid.UUID, period: str) -> UsageQuota | None:
        """取当月额度行。"""
        statement = (
            select(UsageQuota)
            .where(UsageQuota.tenant_id == tenant_id, UsageQuota.period == period)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _get_or_create_quota(self, tenant_id: uuid.UUID, period: str) -> UsageQuota:
        """取当月额度行，没有就建（同一事务内）。"""
        quota = self._get_quota(tenant_id, period)
        if quota is not None:
            return quota
        quota = UsageQuota(
            tenant_id=tenant_id,
            period=period,
            free_quota=self._default_free_quota,
            used_count=0,
            degrade_level="L0",
        )
        self._session.add(quota)
        self._session.flush()
        return quota


def summarize(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """把流水行汇总成「条数 + 成本」（报表/对账用）。"""
    return {
        "count": len(rows),
        "cost_fen": sum(int(row.get("cost_estimate_fen") or 0) for row in rows),
        "sessions": len({row.get("session_id") for row in rows}),
    }
