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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import ContractBundle, get_contracts
from app.domain.ar_ap_service import ArApDelta, ArApService, OverSettleBlocked
from app.domain.ar_ap_service import VersionConflict as ArVersionConflict
from app.domain.doc_service import DocDraft, DocLineDraft, DocService
from app.domain.entity_writer import (
    ENTITY_INTENTS,
    EntityWriteResult,
    EntityWriter,
    UnsupportedSideEffect,
)
from app.domain.intent_registry import IntentRegistry, default_registry, is_pure_entity_intent
from app.domain.inventory_service import (
    InventoryDelta,
    InventoryService,
    NegativeStockBlocked,
)
from app.domain.inventory_service import VersionConflict as InvVersionConflict
from app.models.doc import DocHeader
from app.models.voice import ConfirmCard, VoiceLog
from app.services.audit import AuditService
from app.services.reversal import ReversalOutcome, ReversalService
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
    "WriteOutcome",
]

#: 卡片 TTL（秒）：30s 超时不落账（PRD 3.5【假设】）
CARD_TTL_SECONDS = 30

#: C2 二次语音可接受的说法（**点按钮同样可以**，按钮才是兜底）
CONFIRM_WORDS: tuple[str, ...] = ("确认", "确定", "对", "是的", "没错", "嗯", "记吧", "入账")

#: 撤销窗口（分钟）【假设】
REVERSAL_WINDOW_MINUTES = 10

#: 走「反做单」的意图（``side_effect.inventory_delta == "adjust"``）。
#: 这两个意图**绝不能**走普通单据写入——那会新增一张正数单，账面多出一笔。
REVERSAL_INTENTS: frozenset[str] = frozenset({"sys.undo", "sys.correct"})

#: 由「单据 + 库存 + 应收」这条既有链路负责的实体。
#: 不在这个集合、也不在 :data:`REVERSAL_INTENTS` / :data:`ENTITY_INTENTS` 里的实体，
#: 一律视为「契约声明了但代码没实现」→ 报错，不静默写单。
_DOC_PATH_ENTITIES: frozenset[str] = frozenset({"doc", "inventory", "ar_ap", "payment"})

#: 说「上一单」这一类指代（``DOC`` 槽位没给出具体单号时按「最近一张未撤销单」解析）
_LAST_DOC_WORDS: frozenset[str] = frozenset(
    {"上一单", "上一笔", "上一张", "刚才那单", "刚才那笔", "刚才那张", "刚才的单"}
)

logger = logging.getLogger(__name__)


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
class WriteOutcome:
    """一次确认「到底写了什么」的统一描述。

    三种形态必须用同一个结构描述，否则调用方只能靠 ``doc_id is None`` 猜：
    - 普通交易意图 → 单据（``doc_id`` + ``doc_no`` + 金额）；
    - ``sys.undo`` → 反做单（原单保留，返回反做单号）；
    - 商品/伙伴/价格/词表 → **没有单据**，只有实体（``entity_result``）。
    """

    doc_id: uuid.UUID | None = None
    doc_no: str = ""
    total_amount_fen: int = 0
    detail: str = ""
    entity_result: EntityWriteResult | None = None
    reversal: ReversalOutcome | None = None
    #: 落账之后**还需要用户做一步**时带出的结构化指引（目前只有 ``sys.correct``：
    #: 反做完成 → 等用户重说一句正确的）。``None`` = 一句话就办完了。
    follow_up: dict[str, Any] | None = None


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
    #: 写了实体（商品/伙伴/价格/词表）时的落点——这类意图**不产生单据**
    entity: str | None = None
    entity_id: str | None = None
    summary: str = ""
    #: 反做单（``sys.undo``）：原单保留，这里是反做单 id/号
    reversal_doc_id: str | None = None
    reversal_doc_no: str | None = None
    #: 还需要用户接着做的一步（``sys.correct``：反做已完成 → 请重说一句正确的）
    follow_up: dict[str, Any] | None = None

    @property
    def confirmed(self) -> bool:
        """是否真的写了东西（单据 **或** 实体；只有卡片没有落库不算）。"""
        wrote_something = self.doc_id is not None or self.entity_id is not None
        return wrote_something and self.error_code is None

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
            "reversal_doc_id": self.reversal_doc_id,
            "reversal_doc_no": self.reversal_doc_no,
            "follow_up": dict(self.follow_up) if self.follow_up else None,
            "expired": self.expired,
            "needs_second_voice": self.needs_second_voice,
            "error_code": self.error_code,
            "detail": self.detail,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "summary": self.summary,
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
            # 复活：卡片 id 是**已存在那一行**的 id，视图必须跟着改。
            # 否则视图里带着新 uuid4，用户拿它去确认 → 422「卡片不存在」
            # （同一会话第二次 parse 就必然踩到这条）。
            if str(card.card_id) != str(card_id):
                view = self._builder.build(
                    parse,
                    card_id=str(card.card_id),
                    session_id=str(session_id),
                    asr_text=asr_text,
                    asr_confidence=asr_confidence,
                    impact=preview,
                    lines=lines,
                    conflicts=conflicts,
                    expires_at=expires_at,
                )

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

        # R0：只读意图不该有卡片（create_card 已拦），但**唯一写入口**不能只靠上游保证
        if spec.is_readonly:
            raise AppError(
                "E_VALIDATION",
                message=f"{spec.intent_id} 是 R0 只读：任何时候都不写库、不扣次",
                data={"intent_id": spec.intent_id, "reason": "readonly_intent"},
            )

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
            # 实体的落点存在 card.slots 的**顶层**（与 doc_id 同级），不在 slots 里
            stored_entity = dict(card.slots or {}).get("entity")
            entity = dict(stored_entity) if isinstance(stored_entity, Mapping) else {}
            return ConfirmOutcome(
                card_id=str(card.card_id),
                doc_id=str(existing_doc.doc_id) if existing_doc else None,
                doc_no=str(existing_doc.doc_no) if existing_doc else None,
                card_status="confirmed",
                total_amount_fen=int(existing_doc.total_amount_fen) if existing_doc else 0,
                detail="卡片已确认过，幂等返回首次落账结果（不重复写单、不重复扣次）",
                entity=str(entity.get("entity") or "") or None,
                entity_id=str(entity.get("entity_id") or "") or None,
                summary=str(entity.get("summary") or ""),
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

        # ③ 待确认的槽位/行：**不得静默落账**
        #
        # 卡片自己在 actions 里写着「存在待确认槽位，不得静默落账」，那就必须在
        # 写库前拦住。两类「待确认」都要看：
        # - 槽位级：``source[code] == "pending"`` **且该槽位是必填**
        #   （可选槽位留白是设计如此：``S`` 没说供应商时永远 pending，
        #   若一并拦下，``purchase.create`` 的卡片一出生就不可确认 —— 主链路被守卫锁死，
        #   用户还得手打供应商才能记账。用「先确认黄色项」引导，不阻塞确认）；
        # - 行级：``line["pending"] is True``（例如缺单位换算系数，数量口径不明）。
        #   典型后果：缺「1 箱 = 24 瓶」时 ``qty_base`` 退化成原数量，
        #   **5 箱会记成 5 瓶**——数字自洽、账面全错，用户还看不到提示。
        required_codes = set(spec.required_slots)
        pending_codes = sorted(
            code
            for code, src in payload.source.items()
            if src == "pending" and code in required_codes
        )
        # 纯实体意图（product.create 等）的明细行只是「这次会创建什么」的占位，
        # 商品本来就还不存在 → 一行都不参与行级 pending（否则唯一入口被堵死）
        entity_only = is_pure_entity_intent(spec)
        pending_lines = (
            []
            if entity_only
            else [index for index, line in enumerate(payload.lines, start=1) if line.get("pending")]
        )
        if pending_codes or pending_lines:
            raise AppError(
                "E_VALIDATION",
                message=(
                    "这张卡片还有待确认的内容，不能直接落账："
                    f"待确认槽位 {pending_codes or '无'}，待确认明细行 {pending_lines or '无'}；"
                    "请先把缺的单位/换算系数/对象补齐（点「去确认」或补一句），再确认"
                ),
                data={
                    "reason": "pending_slots",
                    "pending_slots": pending_codes,
                    "pending_lines": pending_lines,
                },
            )

        # ④ 抢占：把卡片从 active/expired **原子地**改成 confirmed。
        #
        # 为什么需要：并发双击（或用户点了两次）时，两个请求都读到 status=='active'，
        # 于是都去 `_apply_side_effects` → **写两张单**。
        # `with_for_update()` 在 SQLite 上不可用，而「先查后改」在任何库上都不是原子操作；
        # 所以用 UPDATE 的 affected-rows 做 CAS：只有一个请求能把 active 改成 confirmed。
        # 放在这里（TTL/C2/pending 三道守卫之后、写库之前）是因为：
        # 前面那些分支都要**原样返回、不能改状态**；而写库失败时整个事务回滚，
        # 状态会跟着回到 active，所以「先标记再写」不会留下脏状态。
        claimed = self._session.execute(
            update(ConfirmCard)
            .where(
                ConfirmCard.tenant_id == tenant_id,
                ConfirmCard.card_id == card.card_id,
                # 只允许 active：把 expired 也算可确认，就等着有人调换守卫顺序后
                # 出现「超时 30s 仍能落账」的口子（TTL 守卫在前，今天是死条件）
                ConfirmCard.status == "active",
            )
            .values(status="confirmed")
        )
        if claimed.rowcount == 0:
            # 没抢到：说明另一个请求已经抢先确认或已处理完 → 幂等返回，**不重复写单**
            self._session.expire(card)
            fresh = self._load_card(tenant_id, card.card_id)
            if str(fresh.status) == "confirmed":
                existing_doc = self._doc_of_card(tenant_id, fresh.card_id)
                return ConfirmOutcome(
                    card_id=str(fresh.card_id),
                    doc_id=str(existing_doc.doc_id) if existing_doc else None,
                    doc_no=str(existing_doc.doc_no) if existing_doc else None,
                    card_status="confirmed",
                    total_amount_fen=int(existing_doc.total_amount_fen) if existing_doc else 0,
                    detail="同一张卡片已被处理过（并发重复点击）：幂等返回首次结果，不重复写单",
                )
            raise AppError(
                "E_VALIDATION",
                message=f"卡片不在可确认状态（{fresh.status}），请刷新后再操作",
                data={"card_status": str(fresh.status)},
            )

        # ⑤ 落账（单据 / 反做单 / 实体三种形态之一）
        outcome = self._apply_side_effects(
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
        if outcome.doc_id is not None:
            confirmed_payload["doc_id"] = str(outcome.doc_id)
            confirmed_payload["doc_no"] = str(outcome.doc_no)
            confirmed_payload["total_amount_fen"] = int(outcome.total_amount_fen)
        if outcome.entity_result is not None:
            confirmed_payload["entity"] = outcome.entity_result.as_dict()
        card.slots = confirmed_payload
        self._session.flush()

        inventory_after = self._inventory_after(tenant_id, store_id, payload)
        audit_entity_id = (
            str(outcome.doc_id)
            if outcome.doc_id is not None
            else (outcome.entity_result.entity_id if outcome.entity_result else str(card.card_id))
        )
        self._audit.record(
            tenant_id=tenant_id,
            action="doc.confirm",
            entity="doc_header" if outcome.doc_id is not None else "entity",
            entity_id=audit_entity_id,
            member_id=member_id,
            detail={
                "card_id": str(card.card_id),
                "intent_id": payload.intent_id,
                "doc_no": str(outcome.doc_no),
                "total_amount_fen": int(outcome.total_amount_fen),
                "confirm_type": confirm_type,
                "button_fallback": bool(button_fallback),
                "entity": outcome.entity_result.as_dict() if outcome.entity_result else None,
                "reversal_doc_no": outcome.reversal.reversal_doc_no if outcome.reversal else None,
            },
        )
        if outcome.detail:
            detail = outcome.detail
        elif self._reversal_window > 0:
            detail = f"已记：{payload.intent_id} {outcome.doc_no}；{self._reversal_window} 分钟内可撤销"
        else:  # pragma: no cover - 撤销窗口关闭时
            detail = f"已记：{payload.intent_id} {outcome.doc_no}"
        return ConfirmOutcome(
            card_id=str(card.card_id),
            doc_id=str(outcome.doc_id) if outcome.doc_id is not None else None,
            doc_no=str(outcome.doc_no) or None,
            card_status="confirmed",
            total_amount_fen=int(outcome.total_amount_fen),
            inventory_after=inventory_after,
            reversal_deadline=(
                moment + timedelta(minutes=self._reversal_window)
                if outcome.reversal is None and outcome.doc_id is not None
                else None
            ),
            detail=detail,
            entity=outcome.entity_result.entity if outcome.entity_result else None,
            entity_id=outcome.entity_result.entity_id if outcome.entity_result else None,
            summary=outcome.entity_result.summary if outcome.entity_result else "",
            reversal_doc_id=outcome.reversal.reversal_doc_id if outcome.reversal else None,
            reversal_doc_no=outcome.reversal.reversal_doc_no if outcome.reversal else None,
            follow_up=outcome.follow_up,
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

    def _apply_side_effects(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        card: ConfirmCard,
        payload: StoredCardPayload,
        spec_intent: str,
        biz_date: date,
    ) -> WriteOutcome:
        """按契约 ``side_effect`` **分派**到三条互不相同的写入路径。

        为什么必须分派（这是本轮修的两类 P0 的共同根因）
        ------------------------------------------------
        之前所有意图都走同一个 ``_write_documents``，于是：
        - ``sys.undo``（``inventory_delta="adjust"``）落成一张**正向单**：
          用户以为撤销了，账面却多出一笔采购；
        - 8 个写实体意图落成一张**零金额空单据**：商品/伙伴/价格根本没写入。

        三条路清清楚楚：反做单 / 实体 / 单据+库存+应收。**没实现的必须报错**，
        绝不静默写单（:class:`UnsupportedSideEffect`）。
        """
        spec = self._registry.require(spec_intent)
        side_effect = dict(spec.side_effect or {})
        entities = tuple(str(entity) for entity in (side_effect.get("entities") or ()))
        inventory_mode = str(side_effect.get("inventory_delta") or "none")

        if spec_intent in REVERSAL_INTENTS or inventory_mode == "adjust":
            return self._write_reversal(
                tenant_id=tenant_id,
                store_id=store_id,
                member_id=member_id,
                payload=payload,
                spec_intent=spec_intent,
                biz_date=biz_date,
            )

        if spec_intent in ENTITY_INTENTS:
            return self._write_entity(
                tenant_id=tenant_id,
                store_id=store_id,
                member_id=member_id,
                payload=payload,
                spec_intent=spec_intent,
            )

        unknown = tuple(
            entity for entity in entities if entity not in _DOC_PATH_ENTITIES
        )
        if unknown:
            # 契约说「要写这些实体」，但没有任何代码实现 → 拒绝，不写空单
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"{spec_intent} 需要写 {list(unknown)}，但这条链路还没有实现；"
                    "为避免「看起来记成功了、实际什么都没写」，本次不落账"
                ),
                data={"reason": "unsupported_side_effect", "intent_id": spec_intent,
                      "entities": list(unknown)},
            )

        doc = self._write_documents(
            tenant_id=tenant_id,
            store_id=store_id,
            member_id=member_id,
            card=card,
            payload=payload,
            spec_intent=spec_intent,
            biz_date=biz_date,
        )
        return WriteOutcome(
            doc_id=doc.doc_id,
            doc_no=str(doc.doc_no),
            total_amount_fen=int(doc.total_amount_fen),
        )

    def _write_entity(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        payload: StoredCardPayload,
        spec_intent: str,
    ) -> WriteOutcome:
        """写实体（商品/别名/伙伴/价格/个人词表）——**不产生单据**。"""
        try:
            result = EntityWriter(self._session, contracts=self._contracts).apply(
                intent_id=spec_intent,
                tenant_id=tenant_id,
                store_id=store_id,
                member_id=member_id,
                slots=payload.slots,
                source=payload.source,
            )
        except UnsupportedSideEffect as exc:
            raise AppError(
                "E_VALIDATION",
                message=f"{spec_intent} 的写入还没有实现，本次不落账（不写空单据）",
                data={"reason": "unsupported_side_effect", "intent_id": exc.intent_id},
            ) from exc
        return WriteOutcome(
            detail=f"已记：{result.summary}",
            entity_result=result,
        )

    def _write_reversal(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        payload: StoredCardPayload,
        spec_intent: str,
        biz_date: date,
    ) -> WriteOutcome:
        """``sys.undo`` / ``sys.correct``：**生成反做单，原单保留**。

        两者共用「反做」这一步，差别只在**做完之后**：

        - ``sys.undo``：反做完成，这件事就结束了；
        - ``sys.correct``：反做只是第一步（契约 ``name_zh`` 写的就是「反做 + 重记」）。
          第二步的「重记」**必须由用户重新说一句**——卡片里只有 ``DOC/TV``
          这类槽位，凑不出可信的明细行，替他猜一个金额就是记一笔他没确认过的账。
          所以这里返回 ``follow_up``，让回执直接告诉他下一句该怎么说。

        上一版在 ``sys.correct`` 分支**直接抛错**，结果是：卡照样建出来、确认 100%
        失败（38 个意图里有 2 个是这种卡）。反做本来就是可执行的第一步，先做掉。
        """
        is_correct = spec_intent == "sys.correct"

        target = self._resolve_target_doc(tenant_id=tenant_id, raw_ref=payload.slots.get("DOC"))
        if target is None:
            raise AppError(
                "E_VALIDATION",
                message=(
                    "没找到要撤销的单据；请说清单号，或直接说「撤销上一单」"
                    if not is_correct
                    else "没找到要更正的原始单据；请说清单号，或直接说「更正上一单」"
                ),
                data={"reason": "reversal_target_not_found", "doc_ref": str(payload.slots.get("DOC") or "")},
            )
        if target.biz_date != biz_date:
            # 不拒绝：撤销的是历史单，biz_date 只是「今天」；但要把差异记进回执
            logger.debug(
                "撤销历史单 doc_no=%s biz_date=%s（今天 %s）",
                target.doc_no,
                target.biz_date,
                biz_date,
            )
        reversal = ReversalService(self._session, registry=self._registry).reverse(
            tenant_id=tenant_id,
            store_id=store_id,
            doc_id=target.doc_id,
            member_id=member_id,
            reason="语音撤销（sys.undo）" if not is_correct else "语音更正第一步：反做原单（sys.correct）",
        )
        detail = (
            f"已撤销 {target.doc_no}：原单保留且标记为「已撤销」，"
            f"另生成反做单 {reversal.reversal_doc_no}（两笔都可查、可导出）"
        )
        follow_up: dict[str, Any] | None = None
        if is_correct:
            # 用户说了「改成 80」时，``TV`` 是**他的意图**，不是可以替他落账的新金额。
            # 原样带进下一步指引：重记时他照着说，账才记得准。
            raw_target = payload.slots.get("TV")
            target_amount_fen = (
                int(raw_target)
                if isinstance(raw_target, int) and not isinstance(raw_target, bool)
                else None
            )
            hint = f"{target.doc_no} 已撤销。请重新说一句正确的，比如「进了三箱可乐，一件三十五」"
            if target_amount_fen:
                hint = (
                    f"{target.doc_no} 已撤销。你说要改成 {target_amount_fen / 100:.2f} 元；"
                    "请重说一句带商品和数量的（比如「进了三箱可乐，一件三十五」），"
                    "我照着新的话记，不替你猜"
                )
            detail = (
                f"更正第一步完成：{target.doc_no} 已反做（反做单 {reversal.reversal_doc_no}）。"
                "现在请重说一句正确的（要说清商品和数量）"
            )
            follow_up = {
                "action": "rerecord",
                "step": 2,
                "total_steps": 2,
                "hint_zh": hint,
                "examples": ["进了三箱可乐，一件三十五", "卖了三瓶可乐，一瓶两块五"],
                "reverted_doc_no": str(target.doc_no),
                "reversal_doc_no": str(reversal.reversal_doc_no),
                "target_amount_fen": target_amount_fen,
            }
        return WriteOutcome(
            doc_id=uuid.UUID(str(reversal.reversal_doc_id)),
            doc_no=str(reversal.reversal_doc_no),
            total_amount_fen=int(target.total_amount_fen),
            detail=detail,
            reversal=reversal,
            follow_up=follow_up,
        )

    def resolve_reversal_target(
        self, *, tenant_id: uuid.UUID, raw_ref: Any
    ) -> DocHeader | None:
        """公开的「这句话能不能反做」预判（**只读**，不写库、不建卡）。

        路由层用它做一件事：**定位不到原单就不建卡**。
        否则用户会拿到一张「确认更正 / 确认撤销」的卡，点下去必 422——
        卡片是承诺，承诺做不到就别给。
        """
        return self._resolve_target_doc(tenant_id=tenant_id, raw_ref=raw_ref)

    def _resolve_target_doc(
        self, *, tenant_id: uuid.UUID, raw_ref: Any
    ) -> DocHeader | None:
        """把 ``DOC`` 槽位解析成一张真实单据（id / 单号 / 「上一单」）。"""
        doc_service = DocService(self._session)
        as_id = _as_uuid(raw_ref)
        if as_id is not None:
            found = doc_service.get(tenant_id=tenant_id, doc_id=as_id)
            if found is not None:
                return found
        text_value = str(raw_ref or "").strip()
        if text_value and not _LAST_DOC_WORDS.intersection({text_value}):
            by_no = doc_service.get_by_no(tenant_id=tenant_id, doc_no=text_value)
            if by_no is not None:
                return by_no
        # 「上一单」/ 空：取最近一张**正常且还没被撤销**的单。
        # 跳过反做单：撤过一次之后，那张 `-R` 单就是最近一张 confirmed，
        # 用户随口再说一句「撤销上一单」就会撤到反做单上（再减一次，必错账）。
        statement = (
            select(DocHeader)
            .where(
                DocHeader.tenant_id == tenant_id,
                DocHeader.status == "confirmed",
                DocHeader.reverses_doc_id.is_(None),
            )
            .order_by(DocHeader.created_at.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

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
        # 同一张卡里同一个商品可能出现多行（「两箱可乐，三箱可乐」是两个 item）。
        # 若每行各做一次 CAS，第二行时库里 version 已 +1 → **永久 1202**，
        # 用户怎么重试都不成。所以先按 product_id 合并数量，只做一次 CAS。
        inv_deltas_by_product: dict[uuid.UUID, InventoryDelta] = {}
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
                merged = inv_deltas_by_product.get(product_id)
                if merged is None:
                    inv_deltas_by_product[product_id] = InventoryDelta(
                        product_id=product_id,
                        qty_base_delta=qty_base * sign,
                        expected_version=versions.get(str(product_id)),
                        snapshot=mode == "snapshot",
                    )
                else:
                    inv_deltas_by_product[product_id] = InventoryDelta(
                        product_id=product_id,
                        qty_base_delta=merged.qty_base_delta + qty_base * sign,
                        expected_version=merged.expected_version,
                        snapshot=merged.snapshot or mode == "snapshot",
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

        inv_deltas = list(inv_deltas_by_product.values())
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
