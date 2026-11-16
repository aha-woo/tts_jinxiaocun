"""影响预演**必须真只读**，而且要能证明（验收③，本轮硬门槛第一条）。

两条证明，缺一条就等于「口头保证」
----------------------------------
1. **内容证明**：``dump 所有表 → 跑一次带边界的预演 → dump → assert A == B``。
   边界必须覆盖负库存、欠款超限、跨门店——预演最容易在这些地方「顺手修一下数据」；
2. **连接证明**：只读会话里**同一句 INSERT** 在普通会话能成功、在只读窗口里必须失败。
   这是 DB 层的事实（SQLite 的 ``PRAGMA query_only`` / PG 的 ``SET TRANSACTION READ ONLY``），
   不是「我没写」的自觉。

另外验证：只读窗口是**窗口**（离开后必须恢复可写），否则会变成「预演一次，之后全库写不进」。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.models.ops import AuditLog
from app.voice.impact_preview import (
    ENTITY_AR_AP,
    ENTITY_INVENTORY,
    CurrentState,
    DeltaLine,
    ImpactPreviewService,
    ReadOnlyViolation,
    simulate,
)
from t04_fixtures import count_rows, dump_snapshot, seed_shop  # noqa: F401  （seed_shop 供手动造库）
from t04_fixtures import seed  # noqa: F401  pytest 夹具

#: 同一句 INSERT：普通会话能成功（证明语句合法），只读窗口里必须失败（证明 DB 拒绝写入）
_INSERT_AUDIT = text(
    "INSERT INTO audit_log (log_id, tenant_id, action, entity) "
    "VALUES (:log_id, :tenant_id, :action, :entity)"
)


def _args(tag: str) -> dict[str, str]:
    """造一组 INSERT 参数（UUID 按 SQLite ``Uuid`` 的 32 位 hex 存法给）。"""
    return {
        "log_id": uuid.uuid4().hex,
        "tenant_id": uuid.uuid4().hex,
        "action": tag,
        "entity": "readonly_proof",
    }


def _edge_deltas(seed_obj) -> tuple[DeltaLine, ...]:
    """三组预演边界：库存变负 / 金额变负 / 不属于本门店的 key。"""
    return (
        DeltaLine(
            entity=ENTITY_INVENTORY,
            key=str(seed_obj.product_id),
            label="可乐 500ml",
            qty_delta_base=Decimal("-9999"),
            unit="瓶",
            mode="decrease",
            calc_trace="卖 9999 瓶（故意超过库存）",
        ),
        DeltaLine(
            entity=ENTITY_AR_AP,
            key=f"{seed_obj.partner_id}:payable",
            label="老张 应付",
            amount_delta_fen=-5000,
            unit="分",
            mode="decrease",
        ),
    )


# ---------------------------------------------------------------- 内容证明：A == B
def test_preview_with_edge_cases_leaves_every_table_unchanged(seed) -> None:
    """A/B 快照逐表相等：预演（含负库存/金额变负/跨门店）不改动任何一行。"""
    database = seed.database
    before = dump_snapshot(database)

    service = ImpactPreviewService(database)
    with database.session(context=seed.context) as session:
        bundle = service.preview(
            tenant_id=seed.tenant_id,
            store_id=seed.store_id,
            deltas=_edge_deltas(seed),
            session=session,
        )
        # 跨门店边界：同样的商品在「二号店」预演，读到的当前值必须是 0（不是主仓的 192）
        cross_store = service.preview(
            tenant_id=seed.tenant_id,
            store_id=seed.other_store_id,
            deltas=_edge_deltas(seed)[:1],
            session=session,
        )

    after = dump_snapshot(database)

    # 预演确实算出了东西（否则 A == B 是「什么都没跑」的空证明）
    assert [item.key for item in bundle.items] == [str(seed.product_id), f"{seed.partner_id}:payable"]
    assert any("库存将变负" in warning for warning in bundle.warnings)
    assert any("金额将变负" in warning for warning in bundle.warnings)
    assert cross_store.items[0].before == Decimal("0"), "预演读到了别的门店的库存"

    # ★ 核心断言：一张表一行都没动
    assert before == after, "预演改动了数据库（只读保证被破坏）"
    diffs = [name for name in before if before[name] != after.get(name)]
    assert diffs == [], f"这些表被预演改动了：{diffs}"


def test_preview_does_not_touch_audit_log_even_though_it_probes(seed) -> None:
    """``is_readonly`` 的探针写必须在 SAVEPOINT 里回滚，不能留下探测行。"""
    database = seed.database
    before = count_rows(database, AuditLog)
    service = ImpactPreviewService(database)
    with service.readonly_session() as readonly:
        assert service.is_readonly(readonly) is True
    assert count_rows(database, AuditLog) == before, "只读探针留下了脏数据"


# ------------------------------------------------------- 连接证明：DB 真的拒写
def test_same_insert_succeeds_normally_but_fails_in_readonly_window(seed) -> None:
    """同一句 INSERT：普通会话成功 / 只读窗口必失败（DB 级证明，不是自觉）。"""
    database = seed.database
    service = ImpactPreviewService(database)

    # ① 普通会话：这句 INSERT 是合法的（证明下面的失败不是因为写错 SQL）
    with database.session(bypass_scope=True) as session:
        session.execute(_INSERT_AUDIT, _args("readonly_proof.normal"))
        session.flush()
    assert count_rows(database, AuditLog) == 1

    # ② 只读会话：同一句 INSERT 必须被 DB 拒绝
    with service.readonly_session() as readonly:
        assert service.is_readonly(readonly) is True, "只读会话居然能写"
        with pytest.raises(Exception) as excinfo:
            readonly.execute(_INSERT_AUDIT, _args("readonly_proof.readonly"))
            readonly.flush()
        assert "readonly" in str(excinfo.value).lower(), f"失败原因不是「只读拒写」：{excinfo.value}"

    # ③ 只读窗口结束：没有任何新行写进来
    assert count_rows(database, AuditLog) == 1, "只读窗口里居然写进了数据"


def test_readonly_window_on_caller_session_is_reset_afterwards(seed) -> None:
    """``readonly_window`` 离开时必须复位 ``query_only``，否则后续写全部失败（最难查的 bug）。"""
    database = seed.database
    service = ImpactPreviewService(database)

    with database.session(context=seed.context) as session:
        with service.readonly_window(session):
            assert service.is_readonly(session) is True
            with pytest.raises(Exception):
                session.execute(_INSERT_AUDIT, _args("readonly_proof.in_window"))
                session.flush()
        # 窗口外：同一会话必须恢复可写
        session.add(AuditLog(tenant_id=seed.tenant_id, action="readonly_proof.after_window", entity="probe"))
        session.flush()

    with database.session(bypass_scope=True) as session:
        actions = [row.action for row in session.execute(select(AuditLog)).scalars()]
    assert "readonly_proof.after_window" in actions, "只读窗口没有复位，连接被永久锁成只读"


def test_pg_refuses_to_open_a_readonly_window_inside_a_write_transaction(seed) -> None:
    """PG 上不允许「在写事务里开只读窗口」——必须显式报错，而不是假装只读。"""
    database = seed.database
    service = ImpactPreviewService(database)
    assert service.dialect == "sqlite"  # 本机无 PG；这条保证由代码路径强制，不由环境决定
    # 直接验证那条守卫存在且可触发（把 is_pg 临时当 True 看它是否拒绝）
    monkey = ImpactPreviewService.__new__(ImpactPreviewService)
    monkey._database = _FakePgDatabase(database)  # type: ignore[attr-defined]
    with database.session(context=seed.context) as session:
        with pytest.raises(ReadOnlyViolation):
            with monkey.readonly_window(session):
                pass


class _FakePgDatabase:
    """把底层 ``Database`` 包装成「自称 PG」的壳，只用于验证那条守卫会拒绝。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    @property
    def is_pg(self) -> bool:
        """强制为真。"""
        return True

    @property
    def dialect_name(self) -> str:
        """方言名。"""
        return "postgresql"

    @property
    def session_factory(self):
        """透传会话工厂。"""
        return self._inner.session_factory


# --------------------------------------------------------- 纯函数：欠款超限边界
def test_simulate_flags_credit_limit_breach_and_never_mutates_input() -> None:
    """欠款超上限必须提示；``simulate`` 是纯函数（输入对象不被改）。"""
    current = (
        CurrentState(
            entity=ENTITY_AR_AP,
            key="p1:receivable",
            label="李婶 应收",
            version=3,
            amount_fen=90_000,
            unit="分",
            credit_limit_fen=100_000,
        ),
        CurrentState(
            entity=ENTITY_INVENTORY,
            key="prod-1",
            label="可乐",
            version=7,
            qty_base=Decimal("192"),
            unit="瓶",
        ),
    )
    deltas = (
        DeltaLine(
            entity=ENTITY_AR_AP,
            key="p1:receivable",
            label="李婶 应收",
            amount_delta_fen=30_000,
            unit="分",
        ),
        DeltaLine(
            entity=ENTITY_INVENTORY,
            key="prod-1",
            label="可乐",
            qty_delta_base=Decimal("48"),
            unit="瓶",
        ),
    )

    bundle = simulate(current, deltas)

    assert current[0].amount_fen == 90_000, "simulate 改了输入对象（不是纯函数）"
    assert bundle.versions == {"p1:receivable": 3, "prod-1": 7}
    assert bundle.preview_version == 7
    ar_item = next(item for item in bundle.items if item.entity == ENTITY_AR_AP)
    assert ar_item.after == Decimal("120000")
    assert any("欠款超上限" in warning for warning in ar_item.warnings)
    qty_item = next(item for item in bundle.items if item.entity == ENTITY_INVENTORY)
    assert qty_item.before == Decimal("192") and qty_item.after == Decimal("240")
    assert qty_item.warnings == ()


def test_simulate_snapshot_mode_overwrites_instead_of_adding() -> None:
    """盘点快照是**设定**不是增量：``after == 数量``，不是「原值 + 数量」。"""
    current = (
        CurrentState(
            entity=ENTITY_INVENTORY, key="prod-1", label="可乐", version=1, qty_base=Decimal("192")
        ),
    )
    bundle = simulate(
        current,
        (DeltaLine(entity=ENTITY_INVENTORY, key="prod-1", label="可乐", qty_delta_base=Decimal("8"), mode="snapshot"),),
    )
    assert bundle.items[0].before == Decimal("192")
    assert bundle.items[0].after == Decimal("8"), "盘点被当成了增量累加"
