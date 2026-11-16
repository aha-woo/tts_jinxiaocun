"""落账服务：**写操作的唯一入口**（H1 / 验收③）。

一条铁律 + 三道校验
------------------
**铁律**：任何写操作必经确认卡片。R0 只读不建卡、不写库、不扣次；
R1/R2 必须卡片；R3 只生成 ``pending_task``，**没有语音直写旁路**。

三道校验（顺序不能换）：

1. **卡片状态与 TTL**：30s 超时 → ``E_CARD_EXPIRED``(1201)，
   **不落账**、进待确认列表、**不重复扣次**（复活复用同一 ``session_id``，
   ``usage_ledger`` 的 ``UNIQUE(tenant_id, session_id, event_type)`` 兜底）；
2. **C2 二次确认**：必须二次语音**或**点兜底按钮，二者有一个就行；
   **任何情况下禁止超时自动执行**；
3. **CAS**：``UPDATE ... WHERE version = 卡片上的版本``，影响 0 行 →
   ``E_PREVIEW_VERSION_CONFLICT``(1202)，**绝不静默覆盖**。

扣次只有一个时机：**卡片生成成功**。它和 ``confirm_card`` 的插入在
**同一个事务**里（:meth:`LedgerService.create_card`），所以不会出现
「卡片没建出来但次已经扣了」。确认、取消、超时、失败**都不再扣次**。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import ContractBundle, get_contracts
from app.domain.ar_ap_service import ArApDelta, ArApService, OverSettleBlocked
from app.domain.ar_ap_service import VersionConflict as ArVersionConflict
from app.domain.doc_service import DocDraft, DocLineDraft, DocService
from app.domain.intent_registry import IntentRegistry, default_registry
from app.domain.inventory_service import (
    InventoryDelta,
    InventoryService,
    NegativeStockBlocked,
)
from app.domain.inventory_service import VersionConflict as InvVersionConflict
from app.models.voice import ConfirmCard, VoiceLog
from app.services.audit import AuditService
from app.services.usage_meter import MeterResult, UsageMeter
from app.voice.card_builder import ConfirmCardBuilder, ConfirmCardView
from app.voice.conflict_detector import Conflict
from app.voice.correction_edit import apply_slot_updates
from app.voice.impact_preview import PreviewBundle, items_from_dicts
from app.voice.output_schema import ParseResult

__all__ = [
    "CARD_TTL_SECONDS",
    "CONFIRM_WORDS",
    "CardCreation",
    "ConfirmOutcome",
    "LedgerService",
    "StoredCardPayload",
]

#: 卡片 TTL（秒）：30s 超时不落账（PRD 3.5【假设】）
CARD_TTL_SECONDS = 30

#: C2 二次语音可接受的说法（**点按钮同样可以**，按钮才是兜底）
CONFIRM_WORDS: tuple[str, ...] = ("确认", "确定", "对", "是的", "没错", "嗯", "记吧", "入账")

#: 撤销窗口（分钟）【假设】
REVERSAL_WINDOW_MINUTES = 10


# ---------------------------------------------------------------------------
# 卡片里持久化的业务载荷
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StoredCardPayload:
    """``confirm_card.slots`` 里持久化的内容（**不是**契约槽位结构）。

    为什么把「落账所需的逐行明细」也存进卡片
    --------------------------------------
    确认时用户可能什么都没改，但我们必须知道「2 箱可乐、48 瓶、¥70」这些**已经算好的**
    数字 —— 重新解析一遍既浪费又可能因为词表变化而算出不同的结果（同一句话两次落账
    金额不同是灾难）。因此卡片生成时就把明细固化下来，确认时**只做 CAS 与写入**。

    ``slots`` 仍是 18 槽位的原始值（回显用），两者都保留，别名不互相覆盖。
    """

    intent_id: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    source: Mapping[str, str] = field(default_factory=dict)
    lines: tuple[Mapping[str, Any], ...] = ()
    asr_text: str = ""
    asr_confidence: float = 0.0
    calc_traces: tuple[str, ...] = ()
    #: 这张卡的 TTL 截止时刻（ISO 8601，UTC）。
    #:
    #: 为什么要落在载荷里，而不是只靠 ``confirm_card.created_at + ttl``：
    #: 复活（30s 超时后重开）复用**同一行**，``created_at`` 不动；若仍按
    #: ``created_at`` 判超时，「复活」就只是把状态改回 active，一确认又立刻判过期，
    #: 卡片永远确认不了。把截止时刻显式存下来，复活时重写它，才有意义。
    expires_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """序列化进 JSON 列。"""
        return {
            "intent_id": self.intent_id,
            "slots": dict(self.slots),
            "source": dict(self.source),
            "lines": [dict(line) for line in self.lines],
            "asr_text": self.asr_text,
            "asr_confidence": self.asr_confidence,
            "calc_traces": list(self.calc_traces),
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | None) -> "StoredCardPayload":
        """从 JSON 列还原。"""
        data = dict(payload or {})
        return cls(
            intent_id=str(data.get("intent_id") or ""),
            slots=dict(data.get("slots") or {}),
            source=dict(data.get("source") or {}),
            lines=tuple(dict(line) for line in data.get("lines") or ()),
            asr_text=str(data.get("asr_text") or ""),
            asr_confidence=float(data.get("asr_confidence") or 0.0),
            calc_traces=tuple(str(trace) for trace in data.get("calc_traces") or ()),
            expires_at=str(data.get("expires_at") or ""),
        )


@dataclass(frozen=True)
class CardCreation:
    """卡片生成结果。"""

    card_id: str
    session_id: str
    view: ConfirmCardView
    preview_version: int
    expires_at: datetime | None
    billed: bool
    duplicated_billing: bool
    meter: MeterResult | None = None
    revoked: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态（7 区块 + 扣次信息）。"""
        payload = self.view.to_payload()
        payload["billing"] = {
            "billed": self.billed,
            "duplicated": self.duplicated_billing,
            "detail": getattr(self.meter, "detail", "") if self.meter else "",
        }
        return payload


@dataclass(frozen=True)
class ConfirmOutcome:
    """确认落账结果。"""

    card_id: str
    doc_id: str | None = None
    doc_no: str | None = None
    card_status: str = "active"
    total_amount_fen: int = 0
    inventory_after: Mapping[str, str] = field(default_factory=dict)
    reversal_deadline: datetime | None = None
    expired: bool = False
    needs_second_voice: bool = False
    error_code: str | None = None
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        """是否真的落了账。"""
        return self.doc_id is not None and self.error_code is None

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "card_id": self.card_id,
            "doc_id": self.doc_id,
            "doc_no": self.doc_no,
            "card_status": self.card_status,
            "confirmed": self.confirmed,
            "total_amount_fen": self.total_amount_fen,
            "inventory_after": dict(self.inventory_after),
            "reversal_deadline": (
                self.reversal_deadline.isoformat() if self.reversal_deadline else None
            ),
            "expired": self.expired,
            "needs_second_voice": self.needs_second_voice,
            "error_code": self.error_code,
            "detail": self.detail,
        }


class LedgerService:
    """卡片 + 落账 + 扣次。"""

    def __init__(
        self,
        session: Session,
        *,
        contracts: ContractBundle | None = None,
        registry: IntentRegistry | None = None,
        card_ttl_seconds: int = CARD_TTL_SECONDS,
        reversal_window_minutes: int = REVERSAL_WINDOW_MINUTES,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话（**写事务**）。
            contracts: 共享契约。
            registry: 意图注册表。
            card_ttl_seconds: 卡片 TTL。
            reversal_window_minutes: 撤销窗口。
        """
        self._session = session
        self._contracts = contracts or get_contracts()
        self._registry = registry or default_registry()
        self._builder = ConfirmCardBuilder(self._contracts, self._registry)
        self._audit = AuditService(session)
        self._card_ttl = int(card_ttl_seconds)
        self._reversal_window = int(reversal_window_minutes)

    # ------------------------------------------------------------------ 建卡
    def create_card(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        session_id: uuid.UUID,
        parse: ParseResult,
        asr_text: str,
        asr_confidence: float,
        lines: Sequence[Mapping[str, Any]],
        preview: PreviewBundle,
        conflicts: Sequence[Conflict] = (),
        now: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> CardCreation:
        """生成确认卡片并**在同一事务里扣一次**。

        Args:
            tenant_id: 租户。
            store_id: 门店。
            member_id: 会员。
            session_id: 语音会话（= 计次幂等键）。
            parse: 已校验的解析结果。
            asr_text: ASR 原文。
            asr_confidence: ASR 置信度。
            lines: 落账明细（逐商品）。
            preview: 只读预演结果。
            conflicts: 冲突结果。
            now: 注入时间。
            idempotency_key: 幂等键（缺省用 ``session_id``）。

        Returns:
            :class:`CardCreation`。

        Raises:
            AppError: R3 → ``E_R3_BLOCKED``；R0 → ``E_VALIDATION``（不该建卡）。
        """
        moment = now or datetime.now(timezone.utc)
        spec = self._registry.require(parse.intent)
        if spec.whitelist_blocked:
            raise AppError(
                "E_R3_BLOCKED",
                message=f"{spec.intent_id} 属 R3 白名单动作：语音只生成待办，不建可执行卡片",
                data={"intent_id": spec.intent_id, "next": "pending_task"},
            )
        if spec.is_readonly:
            raise AppError(
                "E_VALIDATION",
                message=f"{spec.intent_id} 是 R0 只读：不建卡片、不写库、不扣次",
                data={"intent_id": spec.intent_id},
            )

        expires_at = moment + timedelta(seconds=self._card_ttl)
        card_id = uuid.uuid4()
        view = self._builder.build(
            parse,
            card_id=str(card_id),
            session_id=str(session_id),
            asr_text=asr_text,
            asr_confidence=asr_confidence,
            impact=preview,
            lines=lines,
            conflicts=conflicts,
            expires_at=expires_at,
        )

        payload = StoredCardPayload(
            intent_id=parse.intent,
            slots=dict(parse.slots),
            source=dict(parse.source),
            lines=tuple(dict(line) for line in lines),
            asr_text=asr_text,
            asr_confidence=float(asr_confidence),
            calc_traces=tuple(parse.calc_traces),
            expires_at=expires_at.isoformat(),
        )

        card = self._get_card_by_session(tenant_id, session_id)
        if card is None:
            card = ConfirmCard(
                tenant_id=tenant_id,
                card_id=card_id,
                session_id=session_id,
                slots=payload.to_json(),
                impact_preview=[item.as_dict() for item in preview.items],
                calc_traces=list(parse.calc_traces),
                risk_level=spec.risk_level,
                confirm_policy=spec.confirm_policy,
                preview_version=preview.preview_version,
                status="active",
            )
            self._session.add(card)
            try:
                self._session.flush()
            except IntegrityError:
                # 并发建同一会话的卡片：回滚后复用已有卡片（不重复建）
                self._session.rollback()
                card = self._get_card_by_session(tenant_id, session_id)
                if card is None:  # pragma: no cover - 冲突后必然能查到
                    raise
        else:
            # 复活：同一个 session_id 复用同一张卡，保证不重复扣次
            card.slots = payload.to_json()
            card.impact_preview = [item.as_dict() for item in preview.items]
            card.calc_traces = list(parse.calc_traces)
            card.preview_version = preview.preview_version
            card.status = "active"
            self._session.flush()

        self._write_voice_log(
            tenant_id=tenant_id,
            store_id=store_id,
            session_id=session_id,
            asr_text=asr_text,
            asr_confidence=asr_confidence,
            parse_channel=parse.channel or "template",
        )

        # ★ 扣次与建卡**同一事务**：卡片没建出来就一定没扣过
        meter = UsageMeter(self._session)
        meter_result = meter.bill_card(
            tenant_id=tenant_id,
            session_id=session_id,
            card=card,
            now=moment,
        )

        self._audit.record(
            tenant_id=tenant_id,
            action="card.create",
            entity="confirm_card",
            entity_id=str(card.card_id),
            member_id=member_id,
            detail={
                "intent_id": parse.intent,
                "session_id": str(session_id),
                "preview_version": preview.preview_version,
                "billed": meter_result.billed,
                "idempotency_key": idempotency_key or str(session_id),
            },
        )
        return CardCreation(
            card_id=str(card.card_id),
            session_id=str(session_id),
            view=view,
            preview_version=int(card.preview_version),
            expires_at=expires_at,
            billed=bool(meter_result.billed),
            duplicated_billing=bool(meter_result.duplicated),
            meter=meter_result,
        )

    # ------------------------------------------------------------------ 确认
    def confirm(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        card_id: uuid.UUID,
        confirm_type: str = "button",
        spoken_text: str | None = None,
        button_fallback: bool = False,
        biz_date: date | None = None,
        now: datetime | None = None,
    ) -> ConfirmOutcome:
        """确认并落账（**CAS**，非幂等重复点击返回首次结果）。

        Args:
            tenant_id: 租户。
            store_id: 门店。
            member_id: 操作人。
            card_id: 卡片 id。
            confirm_type: ``button`` / ``voice2``。
            spoken_text: 二次语音原话。
            button_fallback: 是否点了按钮兜底（C2 允许按钮代替二次语音）。
            biz_date: 业务日（缺省今天）。
            now: 注入时间。

        Returns:
            :class:`ConfirmOutcome`。

        Raises:
            AppError: ``E_CARD_EXPIRED``(1201) / ``E_PREVIEW_VERSION_CONFLICT``(1202) /
                ``E_R3_BLOCKED``(1205) / ``E_VALIDATION``(422)。
        """
        moment = now or datetime.now(timezone.utc)
        card = self._session.execute(
            select(ConfirmCard)
            .where(ConfirmCard.tenant_id == tenant_id, ConfirmCard.card_id == card_id)
            .limit(1)
        ).scalar_one_or_none()
        if card is None:
            raise AppError("E_VALIDATION", message="卡片不存在或不属于当前租户")

        payload = StoredCardPayload.from_json(card.slots)
        spec = self._registry.require(payload.intent_id)

        # R3：任何路径都不允许直写（卡片本就不该存在，出现了也拦住）
        if spec.whitelist_blocked:
            raise AppError(
                "E_R3_BLOCKED",
                message="R3 白名单动作不能通过语音卡片执行，只能生成待办并在大屏确认",
                data={"intent_id": spec.intent_id},
            )

        # 已确认：幂等返回首次结果（不重复写单）
        if card.status == "confirmed":
            existing_doc = self._doc_of_card(tenant_id, card.card_id)
            return ConfirmOutcome(
                card_id=str(card.card_id),
                doc_id=str(existing_doc.doc_id) if existing_doc else None,
                doc_no=str(existing_doc.doc_no) if existing_doc else None,
                card_status="confirmed",
                total_amount_fen=int(existing_doc.total_amount_fen) if existing_doc else 0,
                detail="卡片已确认过，幂等返回首次落账结果（不重复写单、不重复扣次）",
            )
        if card.status == "cancelled":
            raise AppError("E_VALIDATION", message="卡片已取消，不能确认")

        # ① TTL：30s 超时**不落账**、进待确认、不重复扣次
        if self._is_expired(card, payload, moment):
            card.status = "expired"
            self._session.flush()
            self._audit.record(
                tenant_id=tenant_id,
                action="card.expire",
                entity="confirm_card",
                entity_id=str(card.card_id),
                member_id=member_id,
                detail={"reason": "ttl_30s", "billed": False},
            )
            return ConfirmOutcome(
                card_id=str(card.card_id),
                card_status="expired",
                expired=True,
                error_code="E_CARD_EXPIRED",
                detail=(
                    "卡片已超时（30s）：不落账、进待确认列表；"
                    "复活卡片复用同一 session_id，不会重复扣次"
                ),
            )

        # ② C2：二次语音或按钮兜底，二者有一个就行；禁止超时自动执行
        if spec.confirm_policy == "C2":
            spoken_ok = confirm_type == "voice2" and _is_confirm_word(spoken_text)
            if not spoken_ok and not (confirm_type == "button" and button_fallback):
                return ConfirmOutcome(
                    card_id=str(card.card_id),
                    card_status=str(card.status),
                    needs_second_voice=True,
                    detail=(
                        "该动作需要二次语音确认（说「确认」），也可以直接点界面上的兜底按钮；"
                        "不会因为超时自动执行"
                    ),
                )

        # ③ CAS 落账
        doc = self._write_documents(
            tenant_id=tenant_id,
            store_id=store_id,
            member_id=member_id,
            card=card,
            payload=payload,
            spec_intent=payload.intent_id,
            biz_date=biz_date or moment.date(),
        )
        card.status = "confirmed"
        # 把落账结果写回卡片载荷：重复确认时不用反查审计就能幂等返回
        confirmed_payload = payload.to_json()
        confirmed_payload["doc_id"] = str(doc.doc_id)
        confirmed_payload["doc_no"] = str(doc.doc_no)
        confirmed_payload["total_amount_fen"] = int(doc.total_amount_fen)
        card.slots = confirmed_payload
        self._session.flush()

        inventory_after = self._inventory_after(tenant_id, store_id, payload)
        self._audit.record(
            tenant_id=tenant_id,
            action="doc.confirm",
            entity="doc_header",
            entity_id=str(doc.doc_id),
            member_id=member_id,
            detail={
                "card_id": str(card.card_id),
                "intent_id": payload.intent_id,
                "doc_no": str(doc.doc_no),
                "total_amount_fen": int(doc.total_amount_fen),
                "confirm_type": confirm_type,
                "button_fallback": bool(button_fallback),
            },
        )
        return ConfirmOutcome(
            card_id=str(card.card_id),
            doc_id=str(doc.doc_id),
            doc_no=str(doc.doc_no),
            card_status="confirmed",
            total_amount_fen=int(doc.total_amount_fen),
            inventory_after=inventory_after,
            reversal_deadline=moment + timedelta(minutes=self._reversal_window),
            detail=f"已记：{payload.intent_id} {doc.doc_no}；{self._reversal_window} 分钟内可撤销",
        )

    # ------------------------------------------------------------------ 取消 / 超时 / 复活
    def cancel(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        card_id: uuid.UUID,
        reason: str = "user_cancel",
    ) -> str:
        """取消卡片：**不写业务库、不再扣次**（卡片生成时那一笔不退回）。"""
        card = self._session.execute(
            select(ConfirmCard)
            .where(ConfirmCard.tenant_id == tenant_id, ConfirmCard.card_id == card_id)
            .limit(1)
        ).scalar_one_or_none()
        if card is None:
            raise AppError("E_VALIDATION", message="卡片不存在或不属于当前租户")
        if card.status == "confirmed":
            raise AppError("E_VALIDATION", message="卡片已确认并落账，不能取消；请走撤销")
        card.status = "cancelled"
        self._session.flush()
        self._audit.record(
            tenant_id=tenant_id,
            action="card.cancel",
            entity="confirm_card",
            entity_id=str(card.card_id),
            member_id=member_id,
            detail={"reason": reason, "billed": False},
        )
        return str(card.card_id)

    def revive(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        now: datetime | None = None,
    ) -> CardCreation | None:
        """复活超时卡片：**同一个 session_id**，因此不会重复扣次。"""
        moment = now or datetime.now(timezone.utc)
        card = self._get_card_by_session(tenant_id, session_id)
        if card is None:
            return None
        payload = StoredCardPayload.from_json(card.slots)
        spec = self._registry.require(payload.intent_id)
        if spec.whitelist_blocked:  # pragma: no cover - R3 不会建卡
            raise AppError("E_R3_BLOCKED", message="R3 动作不建可执行卡片")
        new_deadline = moment + timedelta(seconds=self._card_ttl)
        # 复活必须重写截止时刻，否则「刚复活就又过期」（见 StoredCardPayload.expires_at）
        revived_payload = StoredCardPayload(
            intent_id=payload.intent_id,
            slots=dict(payload.slots),
            source=dict(payload.source),
            lines=tuple(dict(line) for line in payload.lines),
            asr_text=payload.asr_text,
            asr_confidence=payload.asr_confidence,
            calc_traces=tuple(payload.calc_traces),
            expires_at=new_deadline.isoformat(),
        )
        parse = ParseResult(
            intent=payload.intent_id,
            slots=dict(payload.slots),
            source=dict(payload.source),
            confidence=payload.asr_confidence or 0.5,
            calc_traces=list(payload.calc_traces),
            route="card",
        )
        card.status = "active"
        card.slots = revived_payload.to_json()
        self._session.flush()
        meter = UsageMeter(self._session)
        result = meter.bill_card(
            tenant_id=tenant_id, session_id=session_id, card=card, now=moment
        )
        view = self._builder.build(
            parse,
            card_id=str(card.card_id),
            session_id=str(session_id),
            asr_text=payload.asr_text,
            asr_confidence=payload.asr_confidence,
            impact=None,
            lines=payload.lines,
            expires_at=new_deadline,
        )
        return CardCreation(
            card_id=str(card.card_id),
            session_id=str(session_id),
            view=view,
            preview_version=int(card.preview_version),
            expires_at=new_deadline,
            billed=bool(result.billed),
            duplicated_billing=bool(result.duplicated),
            meter=result,
            revoked=True,
        )

    # ------------------------------------------------------------------ 重开卡片
    def card_view(self, *, tenant_id: uuid.UUID, card_id: uuid.UUID) -> ConfirmCardView:
        """重开一张卡片（**重新只读预演在路由层做**，这里只重建视图）。

        Raises:
            AppError: ``E_CARD_EXPIRED``(1201) —— 卡片已被回收进待确认列表。
        """
        card = self._load_card(tenant_id, card_id)
        payload = StoredCardPayload.from_json(card.slots)
        if str(card.status) == "cancelled":
            raise AppError("E_VALIDATION", message="卡片已取消，不能再打开")
        parse = ParseResult(
            intent=payload.intent_id,
            slots=dict(payload.slots),
            source=dict(payload.source),
            confidence=payload.asr_confidence or 0.5,
            calc_traces=list(payload.calc_traces),
            route="card",
        )
        bundle = PreviewBundle(
            items=items_from_dicts(card.impact_preview or []),
            versions=_versions_of(card.impact_preview),
        )
        view = self._builder.build(
            parse,
            card_id=str(card.card_id),
            session_id=str(card.session_id),
            asr_text=payload.asr_text,
            asr_confidence=payload.asr_confidence,
            impact=bundle,
            lines=payload.lines,
            expires_at=None,
        )
        return view

    # ------------------------------------------------------------------ 编辑
    def edit_card(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        card_id: uuid.UUID,
        updates: Mapping[str, Any],
        preview: PreviewBundle | None = None,
    ) -> ConfirmCardView:
        """编辑卡片槽位：**本地结构化重算，绝不重调 AI**（验收⑥）。

        Args:
            tenant_id: 租户。
            store_id: 门店。
            member_id: 会员。
            card_id: 卡片 id。
            updates: ``{槽位代号: 新值}``。
            preview: 新的只读预演结果（路由层算好传入）。

        Returns:
            重算后的 :class:`ConfirmCardView`。

        Raises:
            AppError: 卡片已确认/取消（不能再改）。
        """
        card = self._load_card(tenant_id, card_id)
        if str(card.status) in {"confirmed", "cancelled"}:
            raise AppError("E_VALIDATION", message="卡片已确认或已取消，不能再编辑")
        payload = StoredCardPayload.from_json(card.slots)
        bundle = preview or PreviewBundle(
            items=items_from_dicts(card.impact_preview or []),
            versions=_versions_of(card.impact_preview),
        )

        edited = apply_slot_updates(payload=payload, updates=updates)
        card.slots = edited.payload.to_json()
        card.impact_preview = [item.as_dict() for item in bundle.items]
        card.preview_version = bundle.preview_version
        self._session.flush()

        from app.services.correction_feedback import CorrectionFeedbackService

        feedback = CorrectionFeedbackService(self._session)
        for slot_code, old_value, new_value in edited.changes:
            feedback.record(
                tenant_id=tenant_id,
                session_id=uuid.UUID(str(card.session_id)),
                member_id=member_id,
                slot_code=slot_code,
                original_fragment=edited.original_fragment_of(slot_code),
                wrong_value=old_value,
                correct_value=new_value,
                intent_id=edited.payload.intent_id,
                raw_text=edited.payload.asr_text,
            )

        self._audit.record(
            tenant_id=tenant_id,
            action="card.edit",
            entity="confirm_card",
            entity_id=str(card.card_id),
            member_id=member_id,
            detail={
                "changed_slots": [change[0] for change in edited.changes],
                "llm_calls": 0,
                "note": "编辑只在本地重算，未重新调用模型",
            },
        )
        return self.card_view(tenant_id=tenant_id, card_id=card_id)

    def _load_card(self, tenant_id: uuid.UUID, card_id: uuid.UUID) -> ConfirmCard:
        """取卡片实体（不存在即报错）。"""
        card = self._session.execute(
            select(ConfirmCard)
            .where(ConfirmCard.tenant_id == tenant_id, ConfirmCard.card_id == card_id)
            .limit(1)
        ).scalar_one_or_none()
        if card is None:
            raise AppError("E_NOT_FOUND", message="卡片不存在或不属于当前租户")
        return card

    def pending_cards(self, *, tenant_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
        """待确认列表（30s 超时回收的卡片）。"""
        statement = (
            select(ConfirmCard)
            .where(
                ConfirmCard.tenant_id == tenant_id,
                ConfirmCard.status.in_(["expired", "active"]),
            )
            .order_by(ConfirmCard.created_at.desc())
            .limit(max(1, limit))
        )
        rows: list[dict[str, Any]] = []
        for card in self._session.execute(statement).scalars():
            payload = StoredCardPayload.from_json(card.slots)
            rows.append(
                {
                    "card_id": str(card.card_id),
                    "session_id": str(card.session_id),
                    "intent_id": payload.intent_id,
                    "status": str(card.status),
                    "preview_version": int(card.preview_version),
                    "asr_text": payload.asr_text,
                    "billed": False,
                    "note": "复活时复用同一 session_id，不会重复扣次",
                }
            )
        return rows

    # ------------------------------------------------------------------ 内部
    def _get_card_by_session(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> ConfirmCard | None:
        """按会话取卡片（``UNIQUE(tenant_id, session_id)`` 对应）。"""
        return self._session.execute(
            select(ConfirmCard)
            .where(
                ConfirmCard.tenant_id == tenant_id,
                ConfirmCard.session_id == session_id,
            )
            .limit(1)
        ).scalar_one_or_none()

    def _is_expired(
        self, card: ConfirmCard, payload: StoredCardPayload, now: datetime
    ) -> bool:
        """卡片是否已过 TTL（优先用载荷里记下的截止时刻，其次回落到创建时间）。

        用载荷里的 ``expires_at`` 而不是 ``created_at + ttl``，是因为复活复用同一行、
        ``created_at`` 不会变；只看 ``created_at`` 会让复活后的卡片永远「刚复活就过期」。
        """
        deadline = _parse_iso(payload.expires_at)
        if deadline is not None:
            return now >= deadline
        created = card.created_at
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now >= created + timedelta(seconds=self._card_ttl)

    def _write_voice_log(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        session_id: uuid.UUID,
        asr_text: str,
        asr_confidence: float,
        parse_channel: str,
    ) -> None:
        """写 ASR 留痕（**只存文本，不存音频**；一会话一行）。"""
        existing = self._session.execute(
            select(VoiceLog)
            .where(VoiceLog.tenant_id == tenant_id, VoiceLog.session_id == session_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.asr_text = asr_text
            self._session.flush()
            return
        self._session.add(
            VoiceLog(
                tenant_id=tenant_id,
                store_id=store_id,
                session_id=session_id,
                asr_text=asr_text,
                asr_confidence=Decimal(str(round(min(max(asr_confidence, 0.0), 1.0), 3))),
                parse_channel=parse_channel if parse_channel in {"template", "llm", "filtered"} else "template",
            )
        )
        self._session.flush()

    def _write_documents(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        card: ConfirmCard,
        payload: StoredCardPayload,
        spec_intent: str,
        biz_date: date,
    ) -> Any:
        """CAS 写库存/应收 + 写单据（**同一事务**）。

        Raises:
            AppError: ``E_PREVIEW_VERSION_CONFLICT``(1202) / ``E_VALIDATION``(422)。
        """
        side_effect = dict(self._registry.require(spec_intent).side_effect or {})
        versions = _versions_of(card.impact_preview)
        payment_status = str(payload.slots.get("PS") or "已结")

        line_drafts: list[DocLineDraft] = []
        inv_deltas: list[InventoryDelta] = []
        ar_deltas: list[ArApDelta] = []

        for line in payload.lines:
            product_id = _as_uuid(line.get("product_id"))
            if product_id is None:
                raise AppError(
                    "E_VALIDATION",
                    message="卡片里有商品没有落到档案（缺 product_id），不能落账",
                    data={"line": dict(line)},
                )
            qty = _as_decimal(line.get("quantity", line.get("qty", 0)))
            unit = str(line.get("unit") or "")
            qty_base = _as_decimal(line.get("qty_base", qty))
            amount_fen = int(line.get("amount_fen") or 0)
            unit_price_fen = int(line.get("unit_price_fen") or 0)
            line_drafts.append(
                DocLineDraft(
                    product_id=product_id,
                    qty=qty,
                    unit=unit,
                    qty_base=qty_base,
                    unit_price_fen=unit_price_fen,
                    amount_fen=amount_fen,
                    source_map=dict(payload.source),
                )
            )

            mode = str(side_effect.get("inventory_delta") or "none")
            if mode in {"increase", "decrease", "snapshot"}:
                sign = -1 if mode == "decrease" else 1
                inv_deltas.append(
                    InventoryDelta(
                        product_id=product_id,
                        qty_base_delta=qty_base * sign,
                        expected_version=versions.get(str(product_id)),
                        snapshot=mode == "snapshot",
                    )
                )

            ar_mode = str(side_effect.get("ar_ap_delta") or "none")
            partner_id = _as_uuid(line.get("partner_id") or payload.slots.get("C") or payload.slots.get("S"))
            if ar_mode != "none" and partner_id is not None and payment_status != "已结":
                direction = "receivable" if ar_mode.startswith("receivable") else "payable"
                ar_deltas.append(
                    ArApDelta(
                        partner_id=partner_id,
                        direction=direction,
                        amount_fen=amount_fen,
                        settle=ar_mode.endswith("decrease"),
                        expected_version=versions.get(f"{partner_id}:{direction}"),
                    )
                )

        try:
            if inv_deltas:
                InventoryService(self._session).apply(
                    tenant_id=tenant_id, store_id=store_id, deltas=inv_deltas
                )
            if ar_deltas:
                ArApService(self._session).apply(
                    tenant_id=tenant_id, store_id=store_id, deltas=ar_deltas
                )
        except (InvVersionConflict, ArVersionConflict) as exc:
            self._session.rollback()
            raise AppError(
                "E_PREVIEW_VERSION_CONFLICT",
                message=(
                    "这张卡片预演之后，库存/欠款已被别的操作改过；"
                    "请重新打开卡片（会重新只读预演），不要按旧态落账"
                ),
                data={"detail": str(exc)},
            ) from exc
        except NegativeStockBlocked as exc:
            self._session.rollback()
            raise AppError(
                "E_VALIDATION",
                message=str(exc),
                data={"reason": "negative_stock"},
            ) from exc
        except OverSettleBlocked as exc:
            self._session.rollback()
            raise AppError(
                "E_VALIDATION",
                message=str(exc),
                data={"reason": "over_settle"},
            ) from exc

        doc_service = DocService(self._session)
        draft = DocDraft(
            tenant_id=tenant_id,
            store_id=store_id,
            intent_id=spec_intent,
            biz_date=biz_date,
            created_by=member_id,
            payment_status=payment_status,
            lines=tuple(line_drafts),
        )
        return doc_service.create(draft)

    def _inventory_after(
        self, tenant_id: uuid.UUID, store_id: uuid.UUID, payload: StoredCardPayload
    ) -> dict[str, str]:
        """落账后库存快照（回执展示「8 箱 → 10 箱」用）。"""
        service = InventoryService(self._session)
        result: dict[str, str] = {}
        for line in payload.lines:
            product_id = _as_uuid(line.get("product_id"))
            if product_id is None:
                continue
            row = service.get_qty_base(
                tenant_id=tenant_id, store_id=store_id, product_id=product_id
            )
            if row is not None:
                result[str(product_id)] = str(row.qty_base)
        return result

    def _doc_of_card(self, tenant_id: uuid.UUID, card_id: uuid.UUID) -> Any:
        """找这张卡落出来的单据（载荷里直接存了 doc_id；旧数据回落到审计反查）。"""
        card = self._session.execute(
            select(ConfirmCard)
            .where(ConfirmCard.tenant_id == tenant_id, ConfirmCard.card_id == card_id)
            .limit(1)
        ).scalar_one_or_none()
        if card is not None:
            stored = dict(card.slots or {})
            doc_id = _as_uuid(stored.get("doc_id"))
            if doc_id is not None:
                return DocService(self._session).get(tenant_id=tenant_id, doc_id=doc_id)

        from app.models.ops import AuditLog

        rows = (
            self._session.execute(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "doc.confirm",
                    AuditLog.entity == "doc_header",
                )
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            )
            .scalars()
        )
        for row in rows:
            detail = dict(row.detail or {})
            if detail.get("card_id") == str(card_id):
                doc_id = _as_uuid(row.entity_id)
                if doc_id is None:
                    continue
                return DocService(self._session).get(tenant_id=tenant_id, doc_id=doc_id)
        return None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _is_confirm_word(spoken_text: str | None) -> bool:
    """二次语音是否说了可接受的确认词。"""
    text_value = (spoken_text or "").strip()
    if not text_value:
        return False
    return any(word in text_value for word in CONFIRM_WORDS)


def _parse_iso(value: str | None) -> datetime | None:
    """把卡片载荷里的 ISO 时间串转成带时区的 ``datetime``（坏数据返回 ``None``）。"""
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _versions_of(impact_preview: Any) -> dict[str, int]:
    """从卡片预演快照里取每个实体的 version（CAS 期望值）。"""
    versions: dict[str, int] = {}
    items = impact_preview or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:  # pragma: no cover - 数据被写坏时
            items = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "")
        if not key:
            continue
        versions[key] = int(item.get("version") or 0)
    return versions


def _as_uuid(value: Any) -> uuid.UUID | None:
    """安全转 UUID。"""
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _as_decimal(value: Any) -> Decimal:
    """安全转 Decimal（数量不参与 float）。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
