"""单据领域服务：单据号生成、表头/行写入、引用链查询。

三条硬约束
----------
1. **``doc_no`` 租户内唯一且永久不变**（H16）：升级多门店不迁移、不改号。
   生成规则 ``{前缀}{YYYYMMDD}{序号3位}``，冲突时重试（并发下唯一约束是最终裁判）；
2. **单据行数量双写**：``qty`` / ``unit`` 是**原话**（2 箱），``qty_base`` 是**折算**（48 瓶）。
   只存一个的话，「2箱」和「48瓶」回显必然有一个是错的；
3. **不 commit**：与库存/应收/审计同属一个事务（见 :mod:`app.services.ledger`）。

撤销由 :mod:`app.services.reversal` 生成**反做单**——本模块不删任何东西。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.doc import DocHeader, DocLine

__all__ = [
    "DOC_TYPE_BY_INTENT",
    "DocDraft",
    "DocLineDraft",
    "DocService",
    "INTENT_PREFIX",
    "doc_type_of",
    "next_doc_no",
]

#: 意图 → 业务单据类型（``doc.query`` 之类只读意图不会进这里）
DOC_TYPE_BY_INTENT: Mapping[str, str] = {
    "purchase.create": "purchase",
    "purchase.create_multi": "purchase",
    "sales.create": "sales",
    "sales.create_multi": "sales",
    "sales_return.create": "sales_return",
    "purchase_return.create": "purchase_return",
    "stock.take": "stock_take",
    "stock.take_multi": "stock_take",
    "stock.loss": "stock_loss",
    "payment.receive": "payment_receive",
    "payment.pay": "payment_pay",
    "debt.writeoff": "debt_writeoff",
    "price.update": "price_update",
    "product.create": "product_create",
    "product.update": "product_update",
    "product.alias.add": "product_alias_add",
    "product.disable": "product_disable",
    "partner.create": "partner_create",
    "partner.update": "partner_update",
    "sys.correct": "correct",
    "sys.teach": "teach",
}

#: 单据号前缀（人可读、可搜索；不参与任何业务判断）
INTENT_PREFIX: Mapping[str, str] = {
    "purchase": "CG",
    "sales": "XS",
    "sales_return": "XT",
    "purchase_return": "CT",
    "stock_take": "PD",
    "stock_loss": "BS",
    "payment_receive": "SK",
    "payment_pay": "FK",
    "debt_writeoff": "MZ",
    "price_update": "GJ",
    "product_create": "SP",
    "product_update": "SPG",
    "product_alias_add": "BM",
    "product_disable": "SPT",
    "partner_create": "WL",
    "partner_update": "WLG",
    "correct": "GZ",
    "teach": "JX",
}

#: 反做单比原单多一个后缀（一眼看出这是反向单据）
REVERSAL_SUFFIX = "-R"


def doc_type_of(intent_id: str) -> str:
    """意图 → 单据类型（未登记意图用 intent 自身，不猜）。"""
    return DOC_TYPE_BY_INTENT.get(intent_id, intent_id.replace(".", "_"))


@dataclass(frozen=True)
class DocLineDraft:
    """一条单据行（原话数量 + 折算数量双写）。"""

    product_id: uuid.UUID
    qty: Decimal
    unit: str
    qty_base: Decimal
    unit_price_fen: int
    amount_fen: int
    source_map: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DocDraft:
    """一张单据的草稿。"""

    tenant_id: uuid.UUID
    store_id: uuid.UUID
    intent_id: str
    biz_date: date
    created_by: uuid.UUID
    payment_status: str = "已结"
    lines: tuple[DocLineDraft, ...] = ()
    #: 反做单指向的原单
    reversal_of: uuid.UUID | None = None

    @property
    def doc_type(self) -> str:
        """业务单据类型。"""
        return doc_type_of(self.intent_id)

    @property
    def total_amount_fen(self) -> int:
        """合计金额（分）。"""
        return sum(int(line.amount_fen) for line in self.lines)


class DocService:
    """单据读写。"""

    def __init__(self, session: Session) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
        """
        self._session = session

    # ------------------------------------------------------------------ 单号
    def next_doc_no(self, *, tenant_id: uuid.UUID, intent_id: str, biz_date: date) -> str:
        """生成租户内唯一的单据号（``XS20251115001``）。

        并发下靠 ``UNIQUE(tenant_id, doc_no)`` 最终裁决；本方法只在
        **同一天同前缀**已有序号时顺延，绝不重用已用过的号。
        """
        return next_doc_no(
            self._session, tenant_id=tenant_id, intent_id=intent_id, biz_date=biz_date
        )

    # ------------------------------------------------------------------ 写
    def create(self, draft: DocDraft, *, doc_no: str | None = None) -> DocHeader:
        """写入表头 + 全部行（只 flush，不 commit）。

        Args:
            draft: 单据草稿。
            doc_no: 指定单号（反做单 / 幂等重放时用）；缺省自动生成。

        Returns:
            已 flush 的 :class:`DocHeader`。

        Raises:
            IntegrityError: 单号冲突（调用方要么重试、要么判为重复请求）。
        """
        resolved_no = doc_no or self.next_doc_no(
            tenant_id=draft.tenant_id, intent_id=draft.intent_id, biz_date=draft.biz_date
        )
        header = DocHeader(
            tenant_id=draft.tenant_id,
            store_id=draft.store_id,
            doc_no=resolved_no,
            intent_id=draft.intent_id,
            doc_type=draft.doc_type,
            biz_date=draft.biz_date,
            payment_status=draft.payment_status,
            total_amount_fen=draft.total_amount_fen,
            status="confirmed",
            created_by=draft.created_by,
            reverses_doc_id=draft.reversal_of,
        )
        self._session.add(header)
        self._session.flush()
        for line in draft.lines:
            self._session.add(
                DocLine(
                    tenant_id=draft.tenant_id,
                    store_id=draft.store_id,
                    doc_id=header.doc_id,
                    product_id=line.product_id,
                    qty=line.qty,
                    unit=line.unit,
                    qty_base=line.qty_base,
                    unit_price_fen=line.unit_price_fen,
                    amount_fen=line.amount_fen,
                    source_map=dict(line.source_map),
                )
            )
        self._session.flush()
        return header

    # ------------------------------------------------------------------ 读
    def get(self, *, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> DocHeader | None:
        """按 id 取单据。"""
        statement = (
            select(DocHeader)
            .where(DocHeader.tenant_id == tenant_id, DocHeader.doc_id == doc_id)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def get_by_no(self, *, tenant_id: uuid.UUID, doc_no: str) -> DocHeader | None:
        """按单号取单据（语音里说「刚才那张单」时用）。"""
        statement = (
            select(DocHeader)
            .where(DocHeader.tenant_id == tenant_id, DocHeader.doc_no == doc_no)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lines_of(self, *, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> list[DocLine]:
        """取单据全部行。"""
        statement = select(DocLine).where(
            DocLine.tenant_id == tenant_id, DocLine.doc_id == doc_id
        )
        return list(self._session.execute(statement).scalars())

    def list_docs(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID | None = None,
        biz_date: date | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> list[DocHeader]:
        """按条件列单据（R0 只读查询用）。"""
        statement = select(DocHeader).where(DocHeader.tenant_id == tenant_id)
        if store_id is not None:
            statement = statement.where(DocHeader.store_id == store_id)
        if biz_date is not None:
            statement = statement.where(DocHeader.biz_date == biz_date)
        if doc_type is not None:
            statement = statement.where(DocHeader.doc_type == doc_type)
        statement = statement.order_by(DocHeader.created_at.desc()).limit(max(1, limit))
        return list(self._session.execute(statement).scalars())

    def sum_between(
        self,
        *,
        tenant_id: uuid.UUID,
        start: date,
        end: date,
        store_id: uuid.UUID | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> int:
        """区间内单据金额合计（分）——报表用，**只算 confirmed**。"""
        statement = select(func.coalesce(func.sum(DocHeader.total_amount_fen), 0)).where(
            DocHeader.tenant_id == tenant_id,
            DocHeader.status == "confirmed",
            DocHeader.biz_date >= start,
            DocHeader.biz_date <= end,
        )
        if store_id is not None:
            statement = statement.where(DocHeader.store_id == store_id)
        if doc_types:
            statement = statement.where(DocHeader.doc_type.in_(list(doc_types)))
        return int(self._session.execute(statement).scalar_one() or 0)


def next_doc_no(
    session: Session, *, tenant_id: uuid.UUID, intent_id: str, biz_date: date
) -> str:
    """生成 ``{前缀}{YYYYMMDD}{序号}`` 单号（同日同前缀顺延）。

    序号取「同前缀同日已有单数 + 1」，冲突由调用方重试——
    这里**不缓存**、不预分配，避免重启后号段错乱。
    """
    doc_type = doc_type_of(intent_id)
    prefix = INTENT_PREFIX.get(doc_type, "DJ")
    day = biz_date.strftime("%Y%m%d")
    like = f"{prefix}{day}%"
    statement = select(func.count()).select_from(DocHeader).where(
        DocHeader.tenant_id == tenant_id, DocHeader.doc_no.like(like)
    )
    used = int(session.execute(statement).scalar_one() or 0)
    for offset in range(used + 1, used + 200):
        candidate = f"{prefix}{day}{offset:03d}"
        exists = session.execute(
            select(DocHeader.doc_id)
            .where(DocHeader.tenant_id == tenant_id, DocHeader.doc_no == candidate)
            .limit(1)
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise IntegrityError(  # pragma: no cover - 一天 200 单以上才会撞到
        statement="next_doc_no", params={}, orig=RuntimeError("单号段耗尽，请扩大序号位宽")
    )


def plain_text_of(intent_id: str, *, registry: Any = None) -> str:
    """意图中文名（回执与审计用；取不到就回 intent_id，不编）。"""
    if registry is None:
        from app.domain.intent_registry import default_registry

        registry = default_registry()
    spec = registry.get(intent_id)
    return spec.name_zh if spec is not None else intent_id


def today_of(now: datetime | None = None) -> date:
    """业务日（UTC 日期；门店时区在 T05 端侧换算）。"""
    return (now or datetime.utcnow()).date()
