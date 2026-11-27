"""G6 ⑤ 影响预演：**只读连接 + 纯函数 simulate**，物理上写不进去。

三条保证（架构 §2.3 ⑥ / H1 / H9 / D9，破一条整套账就不可信）
---------------------------------------------------------
1. **物理保证**：预演查询走**只读会话**——
   PG 上 ``SET TRANSACTION READ ONLY``，SQLite 上 ``PRAGMA query_only = ON``。
   即使代码写错（误加一句 ``session.add``），DB 层也会直接拒；
   不是靠「我没写」的口头保证。
2. **逻辑保证**：:func:`simulate` 是**无副作用纯函数**，输入只有
   ``current_state + delta``，输出 ``projected_state``。
   纯函数才能被单测穷举边界（负库存、欠款超限、跨门店）。
3. **并发保证**：预演结果携带每个实体的 ``version``，落账时用它做 CAS
   （见 :mod:`app.services.ledger`）；版本不一致 → ``E_PREVIEW_VERSION_CONFLICT``，
   **绝不静默覆盖**。

本模块**只读**：不 ``add``、不 ``flush``、不 ``commit``，也不发任何网络请求。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import Database
from app.models.doc import ArAp
from app.models.inventory import Inventory

__all__ = [
    "CurrentState",
    "DeltaLine",
    "ImpactPreviewService",
    "PreviewBundle",
    "PreviewItem",
    "ReadOnlyViolation",
    "build_deltas",
    "deltas_have_writes",
    "items_from_dicts",
    "simulate",
]

#: 预演实体（只这两个，别的一律不预演——没库存/没欠款的动作不需要预览）
ENTITY_INVENTORY = "inventory"
ENTITY_AR_AP = "ar_ap"

#: 库存变动方向（来自 ``intent.side_effect.inventory_delta``）
INVENTORY_INCREASE = "increase"
INVENTORY_DECREASE = "decrease"
INVENTORY_SNAPSHOT = "snapshot"
INVENTORY_NONE = "none"

#: 应收应付方向（来自 ``intent.side_effect.ar_ap_delta``）
AR_INCREASE = "increase"
AR_DECREASE = "decrease"
AR_NONE = "none"


class ReadOnlyViolation(RuntimeError):
    """在只读会话里尝试写库（DB 层抛出后统一包装成这个异常，便于测试断言）。"""


@dataclass(frozen=True)
class CurrentState:
    """预演输入的「当前状态」（只读查出来的快照）。"""

    entity: str
    key: str
    label: str
    version: int = 0
    qty_base: Decimal = Decimal("0")
    amount_fen: int = 0
    unit: str = ""
    #: 欠款上限（分）：超过就给「欠款超限」提示；``None`` = 不设限
    credit_limit_fen: int | None = None


@dataclass(frozen=True)
class DeltaLine:
    """一条预演改动（来自已校验的 ``ParseResult``，**不是**用户输入原文）。"""

    entity: str
    key: str
    label: str
    qty_delta_base: Decimal = Decimal("0")
    amount_delta_fen: int = 0
    unit: str = ""
    mode: str = INVENTORY_INCREASE
    #: 换算说明（``2箱→48瓶(1箱=24瓶)``），卡片 ④ 区展示
    calc_trace: str | None = None
    #: 待确认标记（缺换算系数 / 多候选）：预演照算，但卡片必须标 pending
    pending: bool = False

    @property
    def kind_of_change(self) -> str:
        """这条改动改的是数量还是金额：库存走数量，应收应付走金额。"""
        return "qty" if self.entity == ENTITY_INVENTORY else "amount"


@dataclass(frozen=True)
class PreviewItem:
    """一条预演结果（卡片 ⑤ 区：``8箱 → 10箱``）。"""

    entity: str
    key: str
    label: str
    before: Decimal
    delta: Decimal
    after: Decimal
    unit: str
    version: int
    kind: str = "qty"
    warnings: tuple[str, ...] = ()
    calc_trace: str | None = None
    pending: bool = False

    @property
    def negative(self) -> bool:
        """预演后是否为负（库存变负 / 应收变负都要显式提示）。"""
        return self.after < 0

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "PreviewItem":
        """从卡片 JSON 还原（复活卡片 / 重新预演对比时用）。"""
        return cls(
            entity=str(row.get("entity") or ""),
            key=str(row.get("key") or ""),
            label=str(row.get("label") or ""),
            before=_as_decimal(row.get("before")),
            delta=_as_decimal(row.get("delta")),
            after=_as_decimal(row.get("after")),
            unit=str(row.get("unit") or ""),
            version=int(row.get("version") or 0),
            kind=str(row.get("kind") or "qty"),
            warnings=tuple(str(item) for item in row.get("warnings") or ()),
            calc_trace=row.get("calc_trace"),
            pending=bool(row.get("pending")),
        )

    def as_dict(self) -> dict[str, Any]:
        """卡片 JSON 形态（``float`` 只用于展示，账务仍用 Decimal/int）。"""
        return {
            "entity": self.entity,
            "key": self.key,
            "label": self.label,
            "before": str(self.before),
            "delta": str(self.delta),
            "after": str(self.after),
            "unit": self.unit,
            "version": self.version,
            "kind": self.kind,
            "warnings": list(self.warnings),
            "calc_trace": self.calc_trace,
            "pending": self.pending,
        }


@dataclass(frozen=True)
class PreviewBundle:
    """一次预演的全部结果。"""

    items: tuple[PreviewItem, ...] = ()
    #: 每个实体的版本（落账 CAS 用）
    versions: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def preview_version(self) -> int:
        """卡片上的单一版本号（取各实体版本最大值；0 表示尚无行）。"""
        return max(self.versions.values()) if self.versions else 0

    @property
    def has_warnings(self) -> bool:
        """是否有任何需要提醒的情况（负库存/欠款超限/待确认）。"""
        return bool(self.warnings) or any(item.warnings for item in self.items)

    def as_dicts(self) -> list[dict[str, Any]]:
        """卡片 ⑤ 区的 JSON。"""
        return [item.as_dict() for item in self.items]


# ---------------------------------------------------------------------------
# 纯函数：无 DB、无副作用
# ---------------------------------------------------------------------------
def simulate(
    current: Sequence[CurrentState],
    deltas: Sequence[DeltaLine],
) -> PreviewBundle:
    """预演核心：**纯函数**，``current + delta -> projected``。

    三条边界必须显式处理，不许「算出来是个负数就当 0 交差」：

    - 库存变负 → ``库存将变负``（真实场景是先卖后补录，允许，但必须提醒）；
    - 欠款超过上限 → ``欠款超限``；
    - ``snapshot`` 模式 → ``after = delta``（**盘点快照设定，不是增量**）；
    - ``none`` 模式 → 不改动，不输出条目（除 ``pending``）。

    Args:
        current: 已读出的当前状态（可为空：说明该实体还没有行）。
        deltas: 改动行。

    Returns:
        :class:`PreviewBundle`（含每个实体的 version 与全部提醒）。
    """
    index: dict[tuple[str, str], CurrentState] = {(row.entity, row.key): row for row in current}
    items: list[PreviewItem] = []
    versions: dict[str, int] = {}
    bundle_warnings: list[str] = []

    for delta in deltas:
        row = index.get((delta.entity, delta.key))
        before_qty = row.qty_base if row is not None else Decimal("0")
        before_amount = row.amount_fen if row is not None else 0
        version = row.version if row is not None else 0
        unit = delta.unit or (row.unit if row is not None else "")
        warnings: list[str] = []

        if delta.kind_of_change == "amount":
            before = Decimal(before_amount)
            applied = Decimal(delta.amount_delta_fen)
            after = applied if delta.mode == INVENTORY_SNAPSHOT else before + applied
            kind = "amount"
        else:
            before = before_qty
            applied = delta.qty_delta_base
            after = applied if delta.mode == INVENTORY_SNAPSHOT else before + applied
            kind = "qty"

        if delta.mode == INVENTORY_NONE and delta.qty_delta_base == 0 and delta.amount_delta_fen == 0:
            # 不改动任何东西的动作（如 product.create）不产出预演条目
            continue

        if kind == "qty" and after < 0:
            warnings.append(f"{delta.label}库存将变负（{_fmt(before)}{unit}→{_fmt(after)}{unit}）")
        if kind == "amount" and after < 0:
            warnings.append(f"{delta.label}金额将变负（{_fmt(before)}→{_fmt(after)} 分）")
        if (
            kind == "amount"
            and delta.entity == ENTITY_AR_AP
            and row is not None
            and row.credit_limit_fen is not None
            and after > Decimal(row.credit_limit_fen)
        ):
            warnings.append(
                f"{delta.label}欠款超上限（{_fmt(after)} > {row.credit_limit_fen} 分）"
            )
        if delta.pending:
            warnings.append(f"{delta.label}待确认（缺换算系数或多候选），不得静默落账")

        items.append(
            PreviewItem(
                entity=delta.entity,
                key=delta.key,
                label=delta.label,
                before=before,
                delta=applied,
                after=after,
                unit=unit,
                version=version,
                kind=kind,
                warnings=tuple(warnings),
                calc_trace=delta.calc_trace,
                pending=delta.pending,
            )
        )
        versions[delta.key] = version
        bundle_warnings.extend(warnings)

    return PreviewBundle(
        items=tuple(items),
        versions=dict(versions),
        warnings=tuple(bundle_warnings),
    )


def items_from_dicts(rows: Iterable[Mapping[str, Any]]) -> tuple[PreviewItem, ...]:
    """把卡片里存的预演快照还原成条目（重开卡片时用）。"""
    items: list[PreviewItem] = []
    for row in rows or ():
        if isinstance(row, Mapping) and row.get("entity"):
            items.append(PreviewItem.from_dict(row))
    return tuple(items)


def _fmt(value: Decimal) -> str:
    """数量展示：去掉小数尾巴（48.0000 → 48）。"""
    text_value = format(value.normalize(), "f") if value == value.to_integral() else str(value)
    return text_value


# ---------------------------------------------------------------------------
# 只读会话 + 读取当前状态
# ---------------------------------------------------------------------------
class ImpactPreviewService:
    """影响预演服务：**只读会话**读当前状态 → 纯函数算 → 返回快照。"""

    def __init__(self, database: Database) -> None:
        """
        Args:
            database: 数据层句柄（只借用它的 session_factory，**不用**事务性 session）。
        """
        self._database = database

    # ------------------------------------------------------------------ 只读会话
    @property
    def dialect(self) -> str:
        """当前方言（决定用哪种只读手段）。"""
        return self._database.dialect_name

    @contextmanager
    def readonly_session(self) -> Iterator[Session]:
        """打开一个**独立的只读会话**（离开时一律 rollback，绝不 commit）。

        - PG：``SET TRANSACTION READ ONLY`` —— 事务级只读，写语句直接报错；
        - SQLite：``PRAGMA query_only = ON`` —— 连接级只读，离开时复位。

        用 ``try/finally`` 复位，是因为 SQLite 的 ``query_only`` 是**连接级**状态，
        不在事务里回滚；忘记复位会让后续正常写全部失败（最难查的那种）。
        """
        session = self._database.session_factory()
        try:
            self._enter_readonly(session)
            yield session
        finally:
            self._exit_readonly(session)
            session.rollback()
            session.close()

    @contextmanager
    def readonly_window(self, session: Session) -> Iterator[Session]:
        """在**已有会话**上开一个只读窗口（用于本地 SQLite 的共享连接）。

        为什么 SQLite 要这个而不是另开连接
        ---------------------------------
        测试与本地用内存库时整个进程共用一条连接（``StaticPool``），另开一个
        Session 会去动**同一个事务**（一 rollback 就把调用方的写一起回滚了）。
        所以在 SQLite 上改为：在调用方会话上临时打开 ``PRAGMA query_only``，
        窗口内的写尝试仍然会被 DB 拒绝（这是连接级保证，不是口头承诺），
        窗口结束立刻复位。

        PG 上**不允许**这种做法：``SET TRANSACTION READ ONLY`` 必须在事务最开始设置，
        在已有写事务里根本开不出来——所以 PG 一律走 :meth:`readonly_session`。
        """
        if self._database.is_pg:
            raise ReadOnlyViolation(
                "PG 上不能在已有写事务里开只读窗口：请传 session=None，"
                "让预演走独立的 READ ONLY 会话"
            )
        self._enter_readonly(session)
        try:
            yield session
        finally:
            self._exit_readonly(session)

    def _enter_readonly(self, session: Session) -> None:
        """开启只读模式（方言相关）。"""
        if self._database.is_pg:
            session.execute(text("SET TRANSACTION READ ONLY"))
        else:
            session.execute(text("PRAGMA query_only = ON"))

    def _exit_readonly(self, session: Session) -> None:
        """复位只读模式（SQLite 需要；PG 的事务结束后自动失效）。"""
        if not self._database.is_pg:
            session.execute(text("PRAGMA query_only = OFF"))

    # ------------------------------------------------------------------ 入口
    def preview(
        self,
        *,
        tenant_id: Any,
        store_id: Any,
        deltas: Sequence[DeltaLine],
        session: Session | None = None,
    ) -> PreviewBundle:
        """执行一次只读预演。

        Args:
            tenant_id: 租户（**显式**传入，不依赖会话上下文，便于跨门店/边界测试）。
            store_id: 门店。
            deltas: 预演改动行（由 :func:`build_deltas` 从解析结果产出）。
            session: 可选——本地 SQLite 可复用调用方会话（走 ``PRAGMA query_only``
                窗口）；PG 上必须传 ``None``，预演走独立 READ ONLY 会话。

        Returns:
            :class:`PreviewBundle`（含 version，供落账 CAS）。

        Raises:
            ReadOnlyViolation: PG 上传了写会话（不允许把只读保证交给自觉）。
        """
        if session is not None:
            with self.readonly_window(session):
                return self._preview_with(session, tenant_id, store_id, deltas)

        with self.readonly_session() as ro_session:
            return self._preview_with(ro_session, tenant_id, store_id, deltas)

    def _preview_with(
        self,
        session: Session,
        tenant_id: Any,
        store_id: Any,
        deltas: Sequence[DeltaLine],
    ) -> PreviewBundle:
        """在给定（只读）会话里读取当前状态并计算。"""
        wanted_products = [delta.key for delta in deltas if delta.entity == ENTITY_INVENTORY]
        wanted_ar = [delta.key for delta in deltas if delta.entity == ENTITY_AR_AP]

        current: list[CurrentState] = []
        if wanted_products:
            current.extend(self._read_inventory(session, tenant_id, store_id, wanted_products))
        if wanted_ar:
            current.extend(self._read_ar_ap(session, tenant_id, store_id, wanted_ar))
        return simulate(current, deltas)

    def _read_inventory(
        self,
        session: Session,
        tenant_id: Any,
        store_id: Any,
        product_keys: Iterable[str],
    ) -> list[CurrentState]:
        """读库存（当前值 + version）。"""
        ids = [uid for uid in (_as_uuid(key) for key in product_keys) if uid]
        if not ids:
            return []
        statement = select(Inventory).where(
            Inventory.tenant_id == tenant_id,
            Inventory.store_id == store_id,
            Inventory.product_id.in_(ids),
        )
        rows: list[CurrentState] = []
        for row in session.execute(statement).scalars():
            rows.append(
                CurrentState(
                    entity=ENTITY_INVENTORY,
                    key=str(row.product_id),
                    label="",
                    version=int(row.version),
                    qty_base=Decimal(row.qty_base),
                )
            )
        return rows

    def _read_ar_ap(
        self,
        session: Session,
        tenant_id: Any,
        store_id: Any,
        keys: Iterable[str],
    ) -> list[CurrentState]:
        """读应收应付（当前未结金额 + version）。

        ``key`` 约定为 ``"<partner_id>:<direction>"``。
        """
        rows: list[CurrentState] = []
        for key in keys:
            partner_id, _, direction = str(key).partition(":")
            partner_uuid = _as_uuid(partner_id)
            if partner_uuid is None:
                continue
            statement = (
                select(ArAp)
                .where(
                    ArAp.tenant_id == tenant_id,
                    ArAp.partner_id == partner_uuid,
                    ArAp.direction == direction,
                    ArAp.status != "settled",
                )
                .order_by(ArAp.created_at.asc())
            )
            open_rows = list(session.execute(statement).scalars())
            outstanding_fen = sum(
                int(r.amount_fen) - int(r.settled_amount_fen) for r in open_rows
            )
            rows.append(
                CurrentState(
                    entity=ENTITY_AR_AP,
                    key=str(key),
                    label="",
                    version=int(open_rows[0].version) if open_rows else 0,
                    amount_fen=outstanding_fen,
                )
            )
        return rows

    # ------------------------------------------------------------------ 只读自检
    def is_readonly(self, session: Session) -> bool:
        """探测该会话是否处于只读模式（**真的写一次看它反不反抗**）。

        不去读 PRAGMA / 事务属性做「看起来对不对」的判断，而是
        在 ``SAVEPOINT`` 里试一次写入：

        - 只读模式下会抛异常 → ``True``；
        - 能写进去 → 立刻回滚到 SAVEPOINT（**不动外层事务**）并返回 ``False``。

        这样「只读」是被 DB 验证过的事实，而不是我们自己的承诺。
        """
        savepoint = None
        try:
            savepoint = session.begin_nested()
            session.execute(
                text(
                    "INSERT INTO audit_log (log_id, tenant_id, action, entity) "
                    "VALUES (:log_id, :tenant_id, :action, :entity)"
                ),
                {
                    "log_id": _probe_uuid(),
                    "tenant_id": _probe_uuid(),
                    "action": "__readonly_probe__",
                    "entity": "__probe__",
                },
            )
            session.flush()
        except Exception:  # noqa: BLE001 - 任何写入失败都说明只读生效
            if savepoint is not None:
                savepoint.rollback()
            return True
        savepoint.rollback()
        return False


def _probe_uuid() -> Any:
    """探测用的 UUID（PG 上是 uuid，SQLite 上 Uuid 类型自行处理）。"""
    import uuid as _uuid

    return _uuid.uuid4()


def _as_uuid(value: Any) -> Any:
    """把字符串 key 转成 UUID；转不了返回 ``None``（不猜）。"""
    import uuid as _uuid

    if isinstance(value, _uuid.UUID):
        return value
    if not value:
        return None
    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 从解析结果构造 delta
# ---------------------------------------------------------------------------
def build_deltas(
    *,
    intent_id: str,
    side_effect: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> tuple[DeltaLine, ...]:
    """把「已校验的解析结果」翻译成预演改动行。

    方向**只从契约** ``intent.side_effect`` 读（``inventory_delta`` / ``ar_ap_delta``），
    不在代码里硬编码「进货=加库存」这类业务常量——契约改了行为自动跟着改（H5）。

    Args:
        intent_id: 意图 id（仅用于计算式文案）。
        side_effect: 契约里的 ``side_effect``。
        lines: 每个商品的 ``{product_id, product_label, qty_base, unit, unit_price_fen,
            amount_fen, partner_id, payment_status, calc_trace, pending}``。
        now: 备用时间注入（当前未使用，保留签名一致性）。

    Returns:
        :class:`DeltaLine` 元组。
    """
    del now  # 签名保留 now，是为了将来「按业务日算欠账龄」时不用改调用方
    inventory_mode = str(side_effect.get("inventory_delta") or INVENTORY_NONE)
    ar_mode = str(side_effect.get("ar_ap_delta") or AR_NONE)

    deltas: list[DeltaLine] = []
    for line in lines:
        product_id = str(line.get("product_id") or "").strip()
        partner_id = str(line.get("partner_id") or "").strip()
        qty_base = _as_decimal(line.get("qty_base"))
        amount_fen = int(line.get("amount_fen") or 0)
        payment_status = str(line.get("payment_status") or "已结")
        label = str(line.get("product_label") or product_id or "库存")

        if inventory_mode in (INVENTORY_INCREASE, INVENTORY_DECREASE, INVENTORY_SNAPSHOT):
            sign = -1 if inventory_mode == INVENTORY_DECREASE else 1
            mode = INVENTORY_SNAPSHOT if inventory_mode == INVENTORY_SNAPSHOT else inventory_mode
            deltas.append(
                DeltaLine(
                    entity=ENTITY_INVENTORY,
                    key=product_id,
                    label=label,
                    qty_delta_base=qty_base * sign,
                    unit=str(line.get("unit") or ""),
                    mode=mode,
                    calc_trace=line.get("calc_trace"),
                    pending=bool(line.get("pending")),
                )
            )

        if ar_mode != AR_NONE and partner_id:
            sign = -1 if ar_mode.endswith("decrease") else 1
            direction = "receivable" if ar_mode.startswith("receivable") else "payable"
            # 已结的普通进销/退货单不产生欠款变动；纯收付款核销（inventory_delta=none 且 *decrease）即使单据标记已结仍扣减往来余额
            is_pure_settle = (inventory_mode == INVENTORY_NONE) and ar_mode.endswith("decrease")
            outstanding = (
                amount_fen * sign
                if (is_pure_settle or payment_status != "已结")
                else 0
            )
            deltas.append(
                DeltaLine(
                    entity=ENTITY_AR_AP,
                    key=f"{partner_id}:{direction}",
                    label=f"{intent_id} 应收应付",
                    amount_delta_fen=outstanding,
                    unit="分",
                    mode=AR_INCREASE if sign > 0 else AR_DECREASE,
                    calc_trace=(
                        f"应付 +{amount_fen} 分" if direction == "payable" else f"应收 +{amount_fen} 分"
                    )
                    if outstanding
                    else "已结，不产生欠款",
                    pending=bool(line.get("pending")),
                )
            )
    return tuple(deltas)


def deltas_have_writes(deltas: Sequence[DeltaLine]) -> bool:
    """这批 delta 是否会产生实际变更（全部为 0 时不算写）。"""
    return any(
        delta.qty_delta_base != 0 or delta.amount_delta_fen != 0 for delta in deltas
    )


def _as_decimal(value: Any) -> Decimal:
    """安全转 Decimal（数量绝不用 float 参与计算）。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 非法数量按 0 处理，由调用方判 pending
        return Decimal("0")


