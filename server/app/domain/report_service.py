"""报表与只读查询领域服务（R0）：**不写库、不占卡片、不扣次**。

R0 的边界（H2）
--------------
``/query/*`` 与 ``/report/*`` 是**只读**分支：解析完直接 ``CLOSED_READ`` 返回结果，
不进 ``AWAIT_CONFIRM``、不建卡片、不写 ``usage_ledger``。
所以本模块里**不允许出现** ``session.add`` / ``flush`` / ``commit``——
所有方法都只做 ``SELECT``。这条约束由 ``tests/test_route_matrix.py`` 与
``tests/test_impact_preview_readonly.py`` 共同守住。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.doc import ArAp, DocHeader, DocLine
from app.models.inventory import Inventory, InventoryMovement
from app.models.partner import Partner
from app.utils.date_utils import today_in_tz
from app.utils.money import format_fen

__all__ = [
    "DebtSummary",
    "DocBrief",
    "ReportService",
    "StockBrief",
    "StockMovement",
    "TodayReport",
]

#: 低库存判定阈值（base_unit）：低于这个数就提醒【假设，需核实】
LOW_STOCK_THRESHOLD = 10

#: 销货/退货类型（算「卖了多少」时用）
SALES_TYPES: tuple[str, ...] = ("sales",)
PURCHASE_TYPES: tuple[str, ...] = ("purchase",)


@dataclass(frozen=True)
class StockBrief:
    """一条库存查询结果。"""

    product_id: str
    product_name: str
    spec: str
    base_unit: str
    qty_base: str
    qty_display: str
    negative_flag: bool = False
    track_batch: bool = False
    avg_cost_fen: int | None = None
    last_purchase_price_fen: int | None = None
    last_sale_price_fen: int | None = None
    status: str = "active"

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "spec": self.spec,
            "base_unit": self.base_unit,
            "qty_base": self.qty_base,
            "qty_display": self.qty_display,
            "negative_flag": self.negative_flag,
            "track_batch": self.track_batch,
            "avg_cost_fen": self.avg_cost_fen,
            "last_purchase_price_fen": self.last_purchase_price_fen,
            "last_sale_price_fen": self.last_sale_price_fen,
            "status": self.status,
        }


@dataclass(frozen=True)
class StockMovement:
    """一条出入库流水。"""

    doc_id: str
    doc_no: str
    doc_type: str
    biz_date: str
    qty_base: str
    unit: str

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "doc_id": self.doc_id,
            "doc_no": self.doc_no,
            "doc_type": self.doc_type,
            "biz_date": self.biz_date,
            "qty_base": self.qty_base,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class DocBrief:
    """一条单据摘要。"""

    doc_id: str
    doc_no: str
    doc_type: str
    intent_id: str
    biz_date: str
    payment_status: str
    status: str
    total_amount_fen: int

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "doc_id": self.doc_id,
            "doc_no": self.doc_no,
            "doc_type": self.doc_type,
            "intent_id": self.intent_id,
            "biz_date": self.biz_date,
            "payment_status": self.payment_status,
            "status": self.status,
            "total_amount_fen": self.total_amount_fen,
            "total_amount_display": format_fen(self.total_amount_fen),
        }


@dataclass(frozen=True)
class DebtSummary:
    """欠款汇总（**按往来单位**，与门店分摊口径分开）。"""

    partner_id: str | None
    partner_name: str
    direction: str
    amount_fen: int
    outstanding_fen: int
    last_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "partner_id": self.partner_id,
            "partner_name": self.partner_name,
            "direction": self.direction,
            "amount_fen": self.amount_fen,
            "outstanding_fen": self.outstanding_fen,
            "outstanding_display": format_fen(self.outstanding_fen),
            "last_date": self.last_date,
        }


@dataclass(frozen=True)
class TodayReport:
    """今日经营快报（R0）。"""

    biz_date: str
    sales_fen: int = 0
    purchase_fen: int = 0
    receivable_open_fen: int = 0
    payable_open_fen: int = 0
    doc_count: int = 0
    gross_profit_fen: int | None = None
    missing_cost_count: int = 0
    top_products: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "biz_date": self.biz_date,
            "sales_fen": self.sales_fen,
            "sales_display": format_fen(self.sales_fen),
            "purchase_fen": self.purchase_fen,
            "purchase_display": format_fen(self.purchase_fen),
            "receivable_open_fen": self.receivable_open_fen,
            "payable_open_fen": self.payable_open_fen,
            "doc_count": self.doc_count,
            "gross_profit_fen": self.gross_profit_fen,
            "gross_profit_display": (
                format_fen(self.gross_profit_fen) if self.gross_profit_fen is not None else None
            ),
            "missing_cost_count": self.missing_cost_count,
            "top_products": [dict(row) for row in self.top_products],
        }


class ReportService:
    """只读查询与报表（R0）。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（**只读使用**）。
        """
        self._session = session

    # ------------------------------------------------------------------ 库存
    def stock_of(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        product_ids: Sequence[uuid.UUID] = (),
        product_name: str | None = None,
        limit: int = 20,
    ) -> list[StockBrief]:
        """查库存（按商品 id 或名称模糊，含草稿态与负库存商品）。"""
        statement = (
            select(Inventory, Product)
            .join(
                Product,
                (Product.product_id == Inventory.product_id)
                & (Product.tenant_id == Inventory.tenant_id),
            )
            .where(
                Inventory.tenant_id == tenant_id,
                Inventory.store_id == store_id,
                Product.status.in_(("draft", "active")),
            )
        )
        if product_ids:
            statement = statement.where(Inventory.product_id.in_(list(product_ids)))
        if product_name:
            statement = statement.where(Product.name.like(f"%{product_name}%"))
        statement = statement.order_by(Inventory.negative_flag.desc(), Product.name.asc()).limit(max(1, limit))
        rows: list[StockBrief] = []
        for inventory, product in self._session.execute(statement).all():
            rows.append(
                StockBrief(
                    product_id=str(product.product_id),
                    product_name=str(product.name),
                    spec=str(product.spec or ""),
                    base_unit=str(product.base_unit),
                    qty_base=str(inventory.qty_base),
                    qty_display=f"{_qty_text(inventory.qty_base)}{product.base_unit}",
                    negative_flag=bool(inventory.negative_flag),
                    track_batch=bool(product.track_batch),
                    avg_cost_fen=int(inventory.avg_cost_fen)
                    if inventory.avg_cost_fen is not None
                    else (int(product.avg_cost_fen) if product.avg_cost_fen is not None else None),
                    last_purchase_price_fen=int(product.last_purchase_price_fen)
                    if product.last_purchase_price_fen is not None
                    else None,
                    last_sale_price_fen=int(product.last_sale_price_fen)
                    if product.last_sale_price_fen is not None
                    else None,
                    status=str(product.status),
                )
            )
        return rows

    def low_stock(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        threshold: int = LOW_STOCK_THRESHOLD,
        limit: int = 20,
    ) -> list[StockBrief]:
        """低库存清单（SQL 过滤 + 优先负库存与最低余量，不漏货）。"""
        statement = (
            select(Inventory, Product)
            .join(
                Product,
                (Product.product_id == Inventory.product_id)
                & (Product.tenant_id == Inventory.tenant_id),
            )
            .where(
                Inventory.tenant_id == tenant_id,
                Inventory.store_id == store_id,
                Product.status.in_(("draft", "active")),
                Inventory.qty_base < func.coalesce(Product.low_stock_qty, threshold),
            )
            .order_by(Inventory.qty_base.asc())
            .limit(max(1, limit))
        )
        rows: list[StockBrief] = []
        for inventory, product in self._session.execute(statement).all():
            rows.append(
                StockBrief(
                    product_id=str(product.product_id),
                    product_name=str(product.name),
                    spec=str(product.spec or ""),
                    base_unit=str(product.base_unit),
                    qty_base=str(inventory.qty_base),
                    qty_display=f"{_qty_text(inventory.qty_base)}{product.base_unit}",
                    negative_flag=bool(inventory.negative_flag),
                    track_batch=bool(product.track_batch),
                    avg_cost_fen=int(inventory.avg_cost_fen)
                    if inventory.avg_cost_fen is not None
                    else (int(product.avg_cost_fen) if product.avg_cost_fen is not None else None),
                    last_purchase_price_fen=int(product.last_purchase_price_fen)
                    if product.last_purchase_price_fen is not None
                    else None,
                    last_sale_price_fen=int(product.last_sale_price_fen)
                    if product.last_sale_price_fen is not None
                    else None,
                    status=str(product.status),
                )
            )
        return rows

    def stock_movement(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        product_id: uuid.UUID,
        limit: int = 20,
    ) -> list[StockMovement]:
        """某商品的出入库流水（直接查唯一数量账本 ``inventory_movement``）。"""
        statement = (
            select(InventoryMovement, DocHeader, Product)
            .outerjoin(
                DocHeader,
                (DocHeader.doc_id == InventoryMovement.doc_id)
                & (DocHeader.tenant_id == InventoryMovement.tenant_id),
            )
            .join(
                Product,
                (Product.product_id == InventoryMovement.product_id)
                & (Product.tenant_id == InventoryMovement.tenant_id),
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.store_id == store_id,
                InventoryMovement.product_id == product_id,
            )
            .order_by(InventoryMovement.occurred_at.desc())
            .limit(max(1, limit))
        )
        rows: list[StockMovement] = []
        for move, header, product in self._session.execute(statement).all():
            rows.append(
                StockMovement(
                    doc_id=str(header.doc_id) if header is not None else "",
                    doc_no=str(header.doc_no) if header is not None else str(move.reason),
                    doc_type=str(header.doc_type) if header is not None else str(move.reason),
                    biz_date=header.biz_date.isoformat()
                    if header is not None
                    else move.occurred_at.date().isoformat(),
                    qty_base=str(move.qty_base),
                    unit=str(product.base_unit),
                )
            )
        return rows

    # ------------------------------------------------------------------ 单据
    def docs(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID | None = None,
        biz_date: date | None = None,
        doc_type: str | None = None,
        limit: int = 20,
    ) -> list[DocBrief]:
        """查单据列表。"""
        statement = select(DocHeader).where(DocHeader.tenant_id == tenant_id)
        if store_id is not None:
            statement = statement.where(DocHeader.store_id == store_id)
        if biz_date is not None:
            statement = statement.where(DocHeader.biz_date == biz_date)
        if doc_type is not None:
            statement = statement.where(DocHeader.doc_type == doc_type)
        statement = statement.order_by(DocHeader.created_at.desc()).limit(max(1, limit))
        return [_brief_of(header) for header in self._session.execute(statement).scalars()]

    # ------------------------------------------------------------------ 欠款
    def debts(
        self,
        *,
        tenant_id: uuid.UUID,
        partner_id: uuid.UUID | None = None,
        direction: str | None = None,
        limit: int = 50,
    ) -> list[DebtSummary]:
        """欠款汇总（**未结**才计入；按往来单位聚合）。"""
        statement = (
            select(ArAp, Partner, DocHeader)
            .outerjoin(
                Partner,
                (Partner.partner_id == ArAp.partner_id) & (Partner.tenant_id == ArAp.tenant_id),
            )
            .outerjoin(
                DocHeader,
                (DocHeader.doc_id == ArAp.doc_id) & (DocHeader.tenant_id == ArAp.tenant_id),
            )
            .where(ArAp.tenant_id == tenant_id, ArAp.status != "settled")
        )
        if partner_id is not None:
            statement = statement.where(ArAp.partner_id == partner_id)
        if direction is not None:
            statement = statement.where(ArAp.direction == direction)
        rows: list[DebtSummary] = []
        # ``.limit()`` 必须打在 **Select** 上（返回 Result 之后再 .limit 会 AttributeError，
        # 「老张欠我多少」「今天卖了多少」这两个每天都问的意图会直接 500）
        for ar_ap, partner, doc in self._session.execute(statement.limit(max(1, limit))).all():
            last_date = (
                str(doc.biz_date)
                if doc is not None
                else (ar_ap.created_at.strftime("%Y-%m-%d") if ar_ap.created_at is not None else None)
            )
            rows.append(
                DebtSummary(
                    partner_id=str(ar_ap.partner_id),
                    partner_name=str(partner.name) if partner is not None else "（未知往来单位）",
                    direction=str(ar_ap.direction),
                    amount_fen=int(ar_ap.amount_fen),
                    outstanding_fen=int(ar_ap.amount_fen) - int(ar_ap.settled_amount_fen),
                    last_date=last_date,
                )
            )
        return rows

    # ------------------------------------------------------------------ 报表
    def _sum_open_debt(self, tenant_id: uuid.UUID, direction: str) -> int:
        return int(
            self._session.execute(
                select(func.coalesce(func.sum(ArAp.amount_fen - ArAp.settled_amount_fen), 0)).where(
                    ArAp.tenant_id == tenant_id,
                    ArAp.direction == direction,
                    ArAp.status != "settled",
                )
            ).scalar_one()
            or 0
        )

    def today(
        self, *, tenant_id: uuid.UUID, store_id: uuid.UUID, biz_date: date
    ) -> TodayReport:
        """今日经营快报（排除反做单，全量汇总应收应付，毛利排除未知成本单据）。"""
        sales = self._sum_docs(tenant_id, store_id, biz_date, SALES_TYPES)
        purchase = self._sum_docs(tenant_id, store_id, biz_date, PURCHASE_TYPES)
        doc_count = int(
            self._session.execute(
                select(func.count())
                .select_from(DocHeader)
                .where(
                    DocHeader.tenant_id == tenant_id,
                    DocHeader.store_id == store_id,
                    DocHeader.biz_date == biz_date,
                    DocHeader.status == "confirmed",
                    DocHeader.reverses_doc_id.is_(None),
                )
            ).scalar_one()
            or 0
        )
        receivable = self._sum_open_debt(tenant_id, "receivable")
        payable = self._sum_open_debt(tenant_id, "payable")

        sales_docs = list(
            self._session.execute(
                select(DocHeader).where(
                    DocHeader.tenant_id == tenant_id,
                    DocHeader.store_id == store_id,
                    DocHeader.biz_date == biz_date,
                    DocHeader.status == "confirmed",
                    DocHeader.doc_type.in_(list(SALES_TYPES)),
                    DocHeader.reverses_doc_id.is_(None),
                )
            ).scalars()
        )
        missing_cost_count = sum(1 for d in sales_docs if d.cogs_fen is None)
        known_docs = [d for d in sales_docs if d.cogs_fen is not None]
        gross_profit_fen = (
            sum(int(d.total_amount_fen) - int(d.cogs_fen or 0) for d in known_docs)
            if known_docs
            else (0 if not sales_docs else None)
        )
        return TodayReport(
            biz_date=biz_date.isoformat(),
            sales_fen=sales,
            purchase_fen=purchase,
            receivable_open_fen=receivable,
            payable_open_fen=payable,
            doc_count=doc_count,
            gross_profit_fen=gross_profit_fen,
            missing_cost_count=missing_cost_count,
            top_products=tuple(self.top_products(tenant_id=tenant_id, store_id=store_id, days=1)),
        )

    def period(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        """区间汇总（周/月报：含毛利、缺成本单数、实收/实付单数与应收应付余额）。"""
        days = max(1, (end - start).days + 1)
        sales_fen = self._sum_docs(tenant_id, store_id, None, SALES_TYPES, start=start, end=end)
        purchase_fen = self._sum_docs(
            tenant_id, store_id, None, PURCHASE_TYPES, start=start, end=end
        )
        doc_count = int(
            self._session.execute(
                select(func.count())
                .select_from(DocHeader)
                .where(
                    DocHeader.tenant_id == tenant_id,
                    DocHeader.store_id == store_id,
                    DocHeader.biz_date >= start,
                    DocHeader.biz_date <= end,
                    DocHeader.status == "confirmed",
                    DocHeader.reverses_doc_id.is_(None),
                )
            ).scalar_one()
            or 0
        )
        sales_docs = list(
            self._session.execute(
                select(DocHeader).where(
                    DocHeader.tenant_id == tenant_id,
                    DocHeader.store_id == store_id,
                    DocHeader.biz_date >= start,
                    DocHeader.biz_date <= end,
                    DocHeader.status == "confirmed",
                    DocHeader.doc_type.in_(list(SALES_TYPES)),
                    DocHeader.reverses_doc_id.is_(None),
                )
            ).scalars()
        )
        missing_cost_count = sum(1 for d in sales_docs if d.cogs_fen is None)
        known_docs = [d for d in sales_docs if d.cogs_fen is not None]
        gross_profit_fen = (
            sum(int(d.total_amount_fen) - int(d.cogs_fen or 0) for d in known_docs)
            if known_docs
            else (0 if not sales_docs else None)
        )
        receivable_open_fen = self._sum_open_debt(tenant_id, "receivable")
        payable_open_fen = self._sum_open_debt(tenant_id, "payable")
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "biz_date": f"{start.isoformat()}~{end.isoformat()}",
            "days": days,
            "sales_fen": sales_fen,
            "sales_display": format_fen(sales_fen),
            "purchase_fen": purchase_fen,
            "purchase_display": format_fen(purchase_fen),
            "receivable_open_fen": receivable_open_fen,
            "payable_open_fen": payable_open_fen,
            "doc_count": doc_count,
            "gross_profit_fen": gross_profit_fen,
            "gross_profit_display": (
                format_fen(gross_profit_fen) if gross_profit_fen is not None else None
            ),
            "missing_cost_count": missing_cost_count,
            "top_products": list(
                self.top_products(tenant_id=tenant_id, store_id=store_id, days=days)
            ),
        }

    def top_products(
        self, *, tenant_id: uuid.UUID, store_id: uuid.UUID, days: int = 7, limit: int = 5
    ) -> list[Mapping[str, Any]]:
        """销量 Top N（只统计正常销售单，排除进货单与反做单）。"""
        start = date.fromisoformat(today_in_tz("Asia/Shanghai")) - timedelta(days=max(1, days) - 1)
        statement = (
            select(
                DocLine.product_id,
                Product.name,
                func.sum(DocLine.qty_base).label("qty"),
                func.sum(func.coalesce(DocLine.amount_fen, 0)).label("amount"),
            )
            .join(
                DocHeader,
                (DocHeader.doc_id == DocLine.doc_id) & (DocHeader.tenant_id == DocLine.tenant_id),
            )
            .outerjoin(
                Product,
                (Product.product_id == DocLine.product_id) & (Product.tenant_id == DocLine.tenant_id),
            )
            .where(
                DocLine.tenant_id == tenant_id,
                DocLine.store_id == store_id,
                DocHeader.status == "confirmed",
                DocHeader.doc_type.in_(list(SALES_TYPES)),
                DocHeader.reverses_doc_id.is_(None),
                DocHeader.biz_date >= start,
            )
            .group_by(DocLine.product_id, Product.name)
            .order_by(func.sum(DocLine.qty_base).desc())
            .limit(max(1, limit))
        )
        rows: list[Mapping[str, Any]] = []
        for product_id, product_name, qty, amount in self._session.execute(statement).all():
            rows.append(
                {
                    "product_id": str(product_id),
                    "product_name": str(product_name or ""),
                    "qty_base": str(qty),
                    "amount_fen": int(amount or 0),
                    "amount_display": format_fen(int(amount or 0)),
                }
            )
        return rows

    # ------------------------------------------------------------------ 内部
    def _sum_docs(
        self,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        biz_date: date | None,
        doc_types: Sequence[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> int:
        """按类型汇总金额。"""
        statement = select(func.coalesce(func.sum(DocHeader.total_amount_fen), 0)).where(
            DocHeader.tenant_id == tenant_id,
            DocHeader.store_id == store_id,
            DocHeader.status == "confirmed",
            DocHeader.doc_type.in_(list(doc_types)),
            # 反做单不算营业额/采购额：原单已被 status='reversed' 排除，
            # 反做单自己必须靠 reverses_doc_id 排除，否则「撤销后今日采购仍是 7000」
            DocHeader.reverses_doc_id.is_(None),
        )
        if biz_date is not None:
            statement = statement.where(DocHeader.biz_date == biz_date)
        if start is not None:
            statement = statement.where(DocHeader.biz_date >= start)
        if end is not None:
            statement = statement.where(DocHeader.biz_date <= end)
        return int(self._session.execute(statement).scalar_one() or 0)


def _brief_of(header: DocHeader) -> DocBrief:
    """单据 → 摘要。"""
    return DocBrief(
        doc_id=str(header.doc_id),
        doc_no=str(header.doc_no),
        doc_type=str(header.doc_type),
        intent_id=str(header.intent_id),
        biz_date=header.biz_date.isoformat(),
        payment_status=str(header.payment_status),
        status=str(header.status),
        total_amount_fen=int(header.total_amount_fen),
    )


def _qty_text(value: Any) -> str:
    """数量展示：``48.0000`` → ``48``。"""
    text_value = str(value)
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return text_value or "0"


def _as_float(value: Any) -> float:
    """仅在**只读展示**里用 float 比较（不参与任何账务计算）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
