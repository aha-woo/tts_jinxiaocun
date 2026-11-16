"""成本护栏：令牌桶 **30/min + 1000/day** 与 ``usage_ledger`` 幂等（验收项⑦）。

三条必须证明的事
----------------
1. **桶真的会拒**：第 31 次要被拒（``E_QUOTA_EXCEEDED``），日桶 1000 次要被拒；
2. **只有卡片成功才计次**：``usage_ledger`` 只能写 ``CARD_CREATED``，
   失败/取消/无意图/超时**不写不扣**，重复确认**不重复扣**；
3. **同事务**：流水与额度自增必须同进同退（回滚即全无），
   否则会出现「扣了次但没卡片」这种最难解释的客诉。

实现侧的硬约束（也是本文件的断言对象）
--------------------------------------
- ``usage_ledger`` 有 ``CHECK (event_type IN ('CARD_CREATED'))``：
  写别的值连数据库都过不去 —— 这是 DDL 级保证，不靠 code review；
- 唯一约束 ``UNIQUE(tenant_id, session_id, event_type)``：并发重复也只成功一次。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.session import Database
from app.models.quota import UsageLedger, UsageQuota
from app.models.tenant import Tenant
from app.services.quota import MemoryQuotaStore, QuotaLimits, QuotaService
from app.services.usage_meter import NON_BILLABLE_REASONS, UsageMeter, current_period

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture()
def database() -> Database:
    """内存库 + 建表（与生产同源模型，SQLite 无 RLS 但约束一致）。"""
    db = Database.from_url("sqlite+pysqlite:///:memory:")
    db.create_all()
    with db.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_ID, name="测试小店", plan_code="free"))
    yield db
    db.dispose()


# --------------------------------------------------------------------------- 桶
def test_minute_bucket_rejects_the_31st_request() -> None:
    """30/min：第 31 次必须被拒（不是「大概」会拒，是精确到第 31 次）。"""
    service = QuotaService(MemoryQuotaStore(), limits=QuotaLimits(per_minute=30, per_day=1000))
    allowed = [service.take("t1", now=1_700_000_000).allowed for _ in range(30)]
    assert all(allowed), "前 30 次应放行"

    rejected = service.take("t1", now=1_700_000_000)
    assert rejected.allowed is False
    assert rejected.scope == "min"
    assert rejected.reason == "rate_limited"
    assert rejected.error_code == "E_QUOTA_EXCEEDED"


def test_day_bucket_rejects_after_1000_and_other_tenants_are_isolated() -> None:
    """1000/day：跨过 1000 后拒；且**不同租户互不影响**。"""
    service = QuotaService(MemoryQuotaStore(), limits=QuotaLimits(per_minute=10_000, per_day=1000))
    for _ in range(1000):
        assert service.take("tenant-a", now=1_700_000_000).allowed is True
    assert service.take("tenant-a", now=1_700_000_000).allowed is False

    # 另一个租户的日桶是独立的
    assert service.take("tenant-b", now=1_700_000_000).allowed is True


def test_minute_bucket_refills_in_the_next_minute() -> None:
    """分钟桶是**窗口**而不是永久计数：下一分钟应重新放行。"""
    service = QuotaService(MemoryQuotaStore(), limits=QuotaLimits(per_minute=1, per_day=10))
    assert service.take("t2", now=1_700_000_000).allowed is True
    assert service.take("t2", now=1_700_000_010).allowed is False
    # 60 秒后进入新的分钟桶
    assert service.take("t2", now=1_700_000_061).allowed is True


def test_quota_decision_carries_snapshot_for_ui() -> None:
    """拒绝时也给出余量快照（前端要显示「今天还能录几次」）。"""
    service = QuotaService(MemoryQuotaStore(), limits=QuotaLimits(per_minute=2, per_day=10))
    service.take("t3", now=1_700_000_000)
    decision = service.take("t3", now=1_700_000_000)
    assert decision.allowed is True and decision.remaining == 0
    assert service.peek("t3", now=1_700_000_000)["minute"] == 0


# --------------------------------------------------------------------------- 计次
def test_card_created_writes_ledger_and_increments_quota(database: Database) -> None:
    """成功的卡片：写 1 条 ``CARD_CREATED`` + 额度 +1（同一事务）。"""
    session_id = uuid.uuid4()
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session, cost_estimate_fen=12)
        result = meter.bill_card(tenant_id=TENANT_ID, session_id=session_id)
        assert result.billed is True
        assert result.used_count == 1
        assert result.wrote_usage_ledger is False if hasattr(result, "wrote_usage_ledger") else True

    with database.session(bypass_scope=True) as session:
        ledger_count = session.execute(select(func.count()).select_from(UsageLedger)).scalar_one()
        assert ledger_count == 1
        quota = session.execute(select(UsageQuota)).scalar_one()
        assert quota.used_count == 1
        assert quota.period == current_period()


def test_duplicate_confirmation_does_not_charge_twice(database: Database) -> None:
    """同一 ``session_id`` 重复确认：返回 ``duplicated``，额度不再 +1。"""
    session_id = uuid.uuid4()
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        first = meter.bill_card(tenant_id=TENANT_ID, session_id=session_id)
        second = meter.bill_card(tenant_id=TENANT_ID, session_id=session_id)
        assert first.billed is True
        assert second.billed is False
        assert second.duplicated is True
        assert second.reason == "duplicated"
        assert second.used_count == 1, "重复确认不得再加额度"

    with database.session(bypass_scope=True) as session:
        assert session.execute(select(func.count()).select_from(UsageLedger)).scalar_one() == 1


def test_non_billable_paths_write_nothing(database: Database) -> None:
    """解析失败 / 取消 / 无意图 / 卡片超时：**不写事件、不扣次**。"""
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        for reason in ("PARSE_FAILED", "CANCELLED", "NO_INTENT", "CARD_TIMEOUT"):
            result = meter.refuse(reason)
            assert result.billed is False
            assert result.reason == reason
            assert result.duplicated is False

    with database.session(bypass_scope=True) as session:
        assert session.execute(select(func.count()).select_from(UsageLedger)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(UsageQuota)).scalar_one() == 0


def test_refuse_rejects_unknown_reason(database: Database) -> None:
    """没登记的「不扣次理由」直接报错，防止有人用 refuse 掩盖真实计费错误。"""
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        assert set(NON_BILLABLE_REASONS) >= {"PARSE_FAILED", "CANCELLED", "NO_INTENT", "CARD_TIMEOUT"}
        with pytest.raises(ValueError):
            meter.refuse("WHATEVER")


def test_ledger_check_constraint_rejects_other_event_types(database: Database) -> None:
    """``event_type`` 只能是 ``CARD_CREATED``（DDL 级约束，写歪都写不进去）。"""
    with database.session(bypass_scope=True) as session:
        session.add(
            UsageLedger(
                tenant_id=TENANT_ID,
                session_id=uuid.uuid4(),
                event_type="PARSE_FAILED",
                cost_estimate_fen=0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        # 触发过 flush 异常后必须先 rollback，否则外层 commit 会抛 PendingRollbackError
        session.rollback()


def test_bill_refuses_non_card_event_via_service(database: Database) -> None:
    """服务层也拦：不给 ``bill_card`` 传错事件类型的机会。"""
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        with pytest.raises(ValueError):
            meter.bill_card(
                tenant_id=TENANT_ID, session_id=uuid.uuid4(), event_type="PARSE_FAILED"
            )


def test_ledger_and_quota_share_one_transaction(database: Database) -> None:
    """回滚即全无：不会出现「扣了次但没有流水」或反过来。"""
    session_id = uuid.uuid4()
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        meter.bill_card(tenant_id=TENANT_ID, session_id=session_id)
        session.rollback()

    with database.session(bypass_scope=True) as session:
        assert session.execute(select(func.count()).select_from(UsageLedger)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(UsageQuota)).scalar_one() == 0


def test_usage_history_is_queryable_and_exportable(database: Database) -> None:
    """流水**可查可导出**（对账/降级排查的硬要求）。"""
    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session, cost_estimate_fen=7)
        for _ in range(3):
            meter.bill_card(tenant_id=TENANT_ID, session_id=uuid.uuid4())

    with database.session(bypass_scope=True) as session:
        meter = UsageMeter(session)
        rows = meter.history()
        assert len(rows) == 3
        assert all(row["event_type"] == "CARD_CREATED" for row in rows)
        export = meter.export()
        assert len(export) == 3
        assert meter.total_cost_fen() == 21
        usage = meter.usage(TENANT_ID)
        assert usage["used_count"] == 3 and usage["remaining"] >= 0
