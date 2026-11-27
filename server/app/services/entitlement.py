"""权益读取服务：**两套口径分别存储、分别展示、不得互相推导**（验收⑦）。

两套口径，别再问「为什么两个数不一样」
----------------------------------
| 口径 | 落库位置 | 含义 | 用途 |
|---|---|---|---|
| **自然月** | ``usage_quota.period = YYYY-MM`` | 本月已用 / 本月可用额度 | 成本归集、降级判定 |
| **30 天滚动** | ``entitlement.valid_from / valid_until`` | 权益有效期 | 什么时候到期、要不要续费 |

它们**天然对不上**：1 号买的 30 天权益在 2 月可能横跨两个自然月，
把两者拼进同一个进度条，用户看到的「还剩 60%」永远是错的。
所以本模块返回**两个独立子对象**，API 也分两个端点（``/entitlement`` 与 ``/usage``）。

**发货只写额度/权益，绝不写 ``usage_ledger``**（A10）——本模块所有写路径都已
断言这一点（:meth:`EntitlementService.assert_no_ledger_write`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ContractBundle, get_contracts
from app.models.pay import Entitlement
from app.models.quota import UsageLedger, UsageQuota
from app.utils.date_utils import today_in_tz

__all__ = [
    "EntitlementService",
    "EntitlementView",
    "UsageView",
    "current_period",
]

#: 自然月归集口径（**不是**权益有效期）
_PERIOD_FORMAT = "%Y-%m"


def current_period(now: datetime | None = None) -> str:
    """自然月 ``YYYY-MM``（中国时区 ``Asia/Shanghai``）。"""
    return today_in_tz("Asia/Shanghai", now=now)[:7]


def _date_only(iso_str: str | None) -> str:
    """ISO 时间戳截取日期部分（YYYY-MM-DD）；空值返回长期有效。"""
    if not iso_str:
        return "长期有效"
    return str(iso_str)[:10]


def _plan_name_of(plan_code: str, quota_granted: int, contracts: ContractBundle) -> str:
    """把内部 plan_code 映射为面向店主的中文套餐名。"""
    if plan_code == "free":
        return "免费体验版"
    for item in contracts.goods.get("goods", []):
        if str(item.get("plan_code") or "") == plan_code and int(item.get("quota_amount") or 0) == quota_granted:
            name_zh = str(item.get("name_zh") or "").strip()
            if name_zh:
                return name_zh
    fallback = {
        "30day": "30 天权益版",
        "quota_pack": "语音加油包",
    }
    return fallback.get(plan_code, plan_code or "免费体验版")


@dataclass(frozen=True)
class EntitlementView:
    """30 天滚动窗口口径（权益有效期）。"""

    plan_code: str
    status: str
    valid_from: str | None
    valid_until: str | None
    days_left: int
    quota_granted: int
    remaining: int
    degrade_level: str
    auto_renew: bool = False
    plan_name: str = ""

    @property
    def expired(self) -> bool:
        """是否已过期（到期**不自动扣费**，靠自有推送提醒复购）。"""
        return self.status == "expired" or self.days_left <= 0

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        resolved_name = self.plan_name or ("免费体验版" if self.plan_code == "free" else self.plan_code)
        return {
            "plan_code": self.plan_code,
            "plan_name": resolved_name,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "valid_until_display": _date_only(self.valid_until),
            "days_left": self.days_left,
            "quota_granted": self.quota_granted,
            "remaining": self.remaining,
            "remaining_quota": self.remaining,
            "degrade_level": self.degrade_level,
            "auto_renew": self.auto_renew,
            "expired": self.expired,
            "caliber": "30 天滚动窗口（权益有效期）",
        }


@dataclass(frozen=True)
class UsageView:
    """自然月口径（用量与额度）。"""

    period: str
    used_count: int
    free_quota: int
    remaining: int
    degrade_level: str

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "period": self.period,
            "used_count": self.used_count,
            "used": self.used_count,
            "free_quota": self.free_quota,
            "quota": self.free_quota,
            "remaining": self.remaining,
            "degrade_level": self.degrade_level,
            "caliber": "自然月 YYYY-MM（用量归集与成本归因）",
        }


class EntitlementService:
    """权益与用量读取（**只读为主**；写只经由发货回调）。"""

    def __init__(
        self,
        session: Session,
        contracts: ContractBundle | None = None,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
            contracts: 共享契约（道具定义真相源）。
        """
        self._session = session
        self._contracts = contracts or get_contracts()

    # ------------------------------------------------------------------ 权益
    def current(
        self, *, tenant_id: uuid.UUID, now: datetime | None = None
    ) -> EntitlementView:
        """当前权益（30 天滚动窗口口径）。"""
        moment = now or datetime.now(timezone.utc)
        row = self._session.execute(
            select(Entitlement)
            .where(Entitlement.tenant_id == tenant_id, Entitlement.status == "active")
            .order_by(Entitlement.valid_until.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return EntitlementView(
                plan_code="free",
                status="active",
                valid_from=None,
                valid_until=None,
                days_left=0,
                quota_granted=0,
                remaining=0,
                degrade_level="L0",
                plan_name="免费体验版",
            )
        valid_until = row.valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        days_left = max(0, (valid_until - moment).days)
        plan_code = str(row.plan_code)
        quota_granted = int(row.quota_granted)
        return EntitlementView(
            plan_code=plan_code,
            status=str(row.status) if days_left > 0 else "expired",
            valid_from=row.valid_from.isoformat(),
            valid_until=row.valid_until.isoformat(),
            days_left=days_left,
            quota_granted=quota_granted,
            remaining=int(row.remaining),
            degrade_level=str(row.degrade_level),
            plan_name=_plan_name_of(plan_code, quota_granted, self._contracts),
        )

    # ------------------------------------------------------------------ 用量
    def usage(
        self, *, tenant_id: uuid.UUID, period: str | None = None, now: datetime | None = None
    ) -> UsageView:
        """当前用量（自然月口径）。"""
        bucket = period or current_period(now)
        row = self._session.execute(
            select(UsageQuota)
            .where(UsageQuota.tenant_id == tenant_id, UsageQuota.period == bucket)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            default_quota = int(self.free_plan().get("quota_amount") or 0)
            return UsageView(
                period=bucket,
                used_count=0,
                free_quota=default_quota,
                remaining=default_quota,
                degrade_level="L0",
            )
        used = int(row.used_count)
        quota = int(row.free_quota)
        return UsageView(
            period=str(row.period),
            used_count=used,
            free_quota=quota,
            remaining=max(0, quota - used),
            degrade_level=str(row.degrade_level),
        )

    # ------------------------------------------------------------------ 道具
    def goods(self) -> list[dict[str, Any]]:
        """道具列表（**价格一律读 ``goods.json``，不硬编码**）。"""
        rows: list[dict[str, Any]] = []
        for goods in self._contracts.goods.get("goods", []):
            rows.append(
                {
                    "goods_id": goods["goods_id"],
                    "goods_type": goods["goods_type"],
                    "name_zh": goods["name_zh"],
                    "plan_code": goods.get("plan_code"),
                    "price_fen": int(goods["price_fen"]),
                    "quota_amount": int(goods.get("quota_amount") or 0),
                    "validity_days": int(goods.get("validity_days") or 0),
                    "status": goods.get("status", "on_shelf"),
                    "benefits_zh": goods.get("benefits_zh", ""),
                    "auto_renew": False,
                    "note": goods.get("note", ""),
                }
            )
        return rows

    def goods_of(self, goods_id: str) -> Mapping[str, Any] | None:
        """按 id 取道具定义（**唯一真相源是 goods.json**）。"""
        for goods in self._contracts.goods.get("goods", []):
            if goods.get("goods_id") == goods_id:
                return goods
        return None

    def free_plan(self) -> Mapping[str, Any]:
        """免费档定义。"""
        return dict(self._contracts.goods.get("free_plan") or {})

    # ------------------------------------------------------------------ 隔离断言
    def assert_no_ledger_write(self, *, tenant_id: uuid.UUID) -> None:
        """断言「发货不写 ``usage_ledger``」这条铁律在数据上也成立。

        Returns:
            无返回值；只做断言。

        Raises:
            AssertionError: 发现非 ``CARD_CREATED`` 的流水行（权益发放被写进了扣次表）。
        """
        others = int(
            self._session.execute(
                select(func.count())
                .select_from(UsageLedger)
                .where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.event_type != "CARD_CREATED",
                )
            ).scalar_one()
            or 0
        )
        assert others == 0, (
            "usage_ledger 出现非 CARD_CREATED 事件：权益发放与语音扣次必须分离（A10）"
        )

    def ledger_count(self, *, tenant_id: uuid.UUID) -> int:
        """当期 ``usage_ledger`` 行数（测试断言「发货后仍为 0」用）。"""
        return int(
            self._session.execute(
                select(func.count())
                .select_from(UsageLedger)
                .where(UsageLedger.tenant_id == tenant_id)
            ).scalar_one()
            or 0
        )


def rolling_window_of(
    *, base: datetime | None, days: int, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """30 天滚动窗口：从「当前有效期末」起算，而非自然月。

    Args:
        base: 当前有效期末（``None`` 表示新开）。
        days: 延长天数。
        now: 现在。

    Returns:
        ``(valid_from, valid_until)``。
    """
    moment = now or datetime.now(timezone.utc)
    start = moment if base is None or base <= moment else base
    return start, start + timedelta(days=max(1, days))
