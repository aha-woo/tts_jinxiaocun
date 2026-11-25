"""纠错回流：用户改卡片 → **本地重算** → ``correction_event`` → 个人词表/候选模板。

两条必须守住的线
-------------
1. **编辑不重调 AI**（验收⑥）：用户把「可乐」改成「娃哈哈」是一次
   **本地结构化更新**，重新解析一遍既浪费钱又可能改出别的结果。
   这条由 ``tests/test_correction_no_llm.py`` 用 ``CountingLlmProvider.calls`` 直接钉住。
2. **回流不能直接生效**：``template_candidate`` 默认 ``pending``，
   必须人工审核后才生效（D6）——否则一次误改就能污染所有人的模板。

``dict_version`` 递增放在 :class:`~app.services.dict_layers.DictVersionCache`，
本模块只负责「记录事实」与「派生候选」。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Product, ProductAlias
from app.models.dict import CorrectionEvent, PersonalDict
from app.models.partner import Partner
from app.models.rule import TemplateCandidate
from app.services.audit import AuditService
from app.services.dict_layers import DictVersionCache

__all__ = [
    "CorrectionOutcome",
    "CorrectionFeedbackService",
    "pattern_hash_of",
]

#: 可回收成词表条目的槽位（只有「说法」类槽位值得记，数量/金额不值得）
TEACHABLE_SLOTS: frozenset[str] = frozenset({"P", "C", "S", "ALIAS", "TF"})


@dataclass(frozen=True)
class CorrectionOutcome:
    """一次纠错的结果。"""

    event_id: str
    slot_code: str
    wrote_personal_dict: bool = False
    wrote_template_candidate: bool = False
    dict_version: int = 0
    detail: str = ""
    learned_rule: dict[str, str] | None = None
    toast_zh: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "event_id": self.event_id,
            "slot_code": self.slot_code,
            "wrote_personal_dict": self.wrote_personal_dict,
            "wrote_template_candidate": self.wrote_template_candidate,
            "dict_version": self.dict_version,
            "detail": self.detail,
            "learned_rule": self.learned_rule,
            "toast_zh": self.toast_zh,
        }


def pattern_hash_of(pattern: str) -> str:
    """模板模式的稳定哈希（去重与回归用）。"""
    return hashlib.sha256(pattern.strip().encode("utf-8")).hexdigest()[:32]


class CorrectionFeedbackService:
    """纠错事件的记录与回流。"""

    def __init__(
        self,
        session: Session,
        *,
        version_cache: DictVersionCache | None = None,
    ) -> None:
        """
        Args:
            session: 已绑定租户上下文的会话。
            version_cache: ``dict_version`` 缓存（缺省用进程内单例语义的新实例）。
        """
        self._session = session
        self._audit = AuditService(session)
        self._versions = version_cache or DictVersionCache()

    # ------------------------------------------------------------------ 记录
    def record(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        member_id: uuid.UUID,
        slot_code: str,
        original_fragment: str | None,
        wrong_value: Any,
        correct_value: Any,
        intent_id: str | None = None,
        raw_text: str | None = None,
    ) -> CorrectionOutcome:
        """记录一次卡片编辑（**本地重算，不重调 AI**）。

        Args:
            tenant_id: 租户。
            session_id: 语音会话。
            member_id: 会员。
            slot_code: 被改的槽位。
            original_fragment: 原话里对应的片段。
            wrong_value: 改前的值。
            correct_value: 改后的值。
            intent_id: 意图（用于生成候选模板）。
            raw_text: 整句原话（用于生成候选模板）。

        Returns:
            :class:`CorrectionOutcome`。
        """
        event = CorrectionEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            slot_code=slot_code,
            original_fragment=original_fragment,
            wrong_value={"value": _plain(wrong_value)},
            correct_value={"value": _plain(correct_value)},
        )
        self._session.add(event)
        self._session.flush()

        wrote_dict = False
        if slot_code in TEACHABLE_SLOTS and original_fragment and raw_text is not None:
            wrote_dict = self._upsert_personal_dict(
                tenant_id=tenant_id,
                member_id=member_id,
                raw_text=original_fragment.strip(),
                mapped_type=_mapped_type_of(slot_code),
                mapped_id=None,
            )

        learned_rule: dict[str, str] | None = None
        toast_zh: str | None = None
        wrong_str = str(wrong_value).strip() if wrong_value not in (None, "") else ""
        correct_str = str(correct_value).strip() if correct_value not in (None, "") else ""
        if slot_code in {"P", "C", "S"} and wrong_str and correct_str and wrong_str != correct_str:
            repeat_count = self._count_same_correction(
                tenant_id=tenant_id,
                slot_code=slot_code,
                wrong_str=wrong_str,
                correct_str=correct_str,
            )
            if repeat_count >= 2:
                resolved_id = self._resolve_mapped_id(
                    tenant_id=tenant_id,
                    slot_code=slot_code,
                    correct_value=correct_value,
                )
                promoted = self._upsert_personal_dict(
                    tenant_id=tenant_id,
                    member_id=member_id,
                    raw_text=wrong_str,
                    mapped_type=_mapped_type_of(slot_code),
                    mapped_id=resolved_id,
                )
                if slot_code == "P" and resolved_id is not None:
                    self._ensure_product_alias(
                        tenant_id=tenant_id,
                        member_id=member_id,
                        product_id=resolved_id,
                        alias_text=wrong_str,
                    )
                if promoted:
                    wrote_dict = True
                learned_rule = {"from": wrong_str, "to": correct_str}
                toast_zh = f"记住了：{wrong_str} → {correct_str}"

        wrote_candidate = False
        if intent_id and raw_text:
            wrote_candidate = self._upsert_template_candidate(
                tenant_id=tenant_id,
                intent_id=intent_id,
                raw_text=raw_text,
                slot_code=slot_code,
                correct_value=correct_value,
                member_id=member_id,
            )

        # 教了就必须击穿缓存：版本 +1
        version = self._versions.bump(str(tenant_id), str(member_id)) if (wrote_dict or wrote_candidate) else self._versions.get(str(tenant_id), str(member_id))
        if learned_rule is not None:
            from app.services.voice_runtime import invalidate_shared_prompt_cache

            invalidate_shared_prompt_cache(str(tenant_id))

        self._audit.record(
            tenant_id=tenant_id,
            action="correction.record",
            entity="correction_event",
            entity_id=str(event.event_id),
            member_id=member_id,
            detail={
                "slot_code": slot_code,
                "intent_id": intent_id,
                "wrote_personal_dict": wrote_dict,
                "wrote_template_candidate": wrote_candidate,
                "dict_version": version,
                "learned_rule": learned_rule,
                "note": "编辑只在本地重算，未重新调用模型",
            },
        )
        return CorrectionOutcome(
            event_id=str(event.event_id),
            slot_code=slot_code,
            wrote_personal_dict=wrote_dict,
            wrote_template_candidate=wrote_candidate,
            dict_version=version,
            detail="已记录纠错；编辑为本地重算（0 次模型调用）",
            learned_rule=learned_rule,
            toast_zh=toast_zh,
        )

    def history(
        self, *, tenant_id: uuid.UUID, session_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[CorrectionEvent]:
        """查纠错历史。"""
        statement = select(CorrectionEvent).where(CorrectionEvent.tenant_id == tenant_id)
        if session_id is not None:
            statement = statement.where(CorrectionEvent.session_id == session_id)
        statement = statement.order_by(CorrectionEvent.created_at.desc()).limit(max(1, limit))
        return list(self._session.execute(statement).scalars())

    # ------------------------------------------------------------------ 内部
    def _resolve_mapped_id(
        self, *, tenant_id: uuid.UUID, slot_code: str, correct_value: Any
    ) -> uuid.UUID | None:
        text = str(correct_value or "").strip()
        if not text:
            return None
        if slot_code in {"P", "ALIAS"}:
            name_only = text.split(" ")[0]
            prod = self._session.execute(
                select(Product)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.status.in_(("draft", "active")),
                    Product.name.in_((text, name_only)),
                )
                .limit(1)
            ).scalar_one_or_none()
            return prod.product_id if prod is not None else None
        if slot_code in {"C", "S"}:
            partner = self._session.execute(
                select(Partner)
                .where(
                    Partner.tenant_id == tenant_id,
                    Partner.status == "active",
                    Partner.name == text,
                )
                .limit(1)
            ).scalar_one_or_none()
            return partner.partner_id if partner is not None else None
        return None

    def _count_same_correction(
        self, *, tenant_id: uuid.UUID, slot_code: str, wrong_str: str, correct_str: str
    ) -> int:
        rows = self._session.execute(
            select(CorrectionEvent)
            .where(
                CorrectionEvent.tenant_id == tenant_id,
                CorrectionEvent.slot_code == slot_code,
            )
            .order_by(CorrectionEvent.created_at.desc())
            .limit(100)
        ).scalars()
        count = 0
        for row in rows:
            w = (row.wrong_value or {}).get("value")
            c = (row.correct_value or {}).get("value")
            if str(w or "").strip() == wrong_str and str(c or "").strip() == correct_str:
                count += 1
        return count

    def _ensure_product_alias(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        product_id: uuid.UUID,
        alias_text: str,
    ) -> None:
        cleaned = (alias_text or "").strip()
        if not cleaned or len(cleaned) > 64:
            return
        existing = self._session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.tenant_id == tenant_id,
                ProductAlias.member_id == member_id,
                ProductAlias.alias_text == cleaned,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(
                ProductAlias(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    member_id=member_id,
                    alias_text=cleaned,
                    source="personal",
                )
            )
            self._session.flush()

    def _upsert_personal_dict(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        raw_text: str,
        mapped_type: str,
        mapped_id: uuid.UUID | None,
    ) -> bool:
        """写/更新个人词表（同一说法不重复建）。"""
        existing = self._session.execute(
            select(PersonalDict)
            .where(
                PersonalDict.tenant_id == tenant_id,
                PersonalDict.member_id == member_id,
                PersonalDict.raw_text == raw_text,
                PersonalDict.mapped_type == mapped_type,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.version = int(existing.version) + 1
            if mapped_id is not None:
                existing.mapped_id = mapped_id
            self._session.flush()
            return True
        self._session.add(
            PersonalDict(
                tenant_id=tenant_id,
                member_id=member_id,
                layer="personal",
                raw_text=raw_text,
                mapped_type=mapped_type,
                mapped_id=mapped_id,
                version=1,
            )
        )
        self._session.flush()
        return True

    def _upsert_template_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        intent_id: str,
        raw_text: str,
        slot_code: str,
        correct_value: Any,
        member_id: uuid.UUID,
    ) -> bool:
        """生成候选模板（**默认 pending，必须人工审核**）。"""
        pattern = _pattern_of(raw_text, slot_code, correct_value)
        digest = pattern_hash_of(f"{intent_id}|{pattern}")
        existing = self._session.execute(
            select(TemplateCandidate)
            .where(
                TemplateCandidate.tenant_id == tenant_id,
                TemplateCandidate.intent_id == intent_id,
                TemplateCandidate.pattern_hash == digest,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return False
        self._session.add(
            TemplateCandidate(
                tenant_id=tenant_id,
                intent_id=intent_id,
                pattern=pattern,
                pattern_hash=digest,
                version=1,
                status="pending",
                created_by=member_id,
            )
        )
        self._session.flush()
        return True


def _pattern_of(raw_text: str, slot_code: str, correct_value: Any) -> str:
    """把「原话 + 槽位修正」压成一个可读的候选模板。"""
    return f"{raw_text.strip()} => {slot_code}={_plain(correct_value)}"


def _mapped_type_of(slot_code: str) -> str:
    """槽位 → 词表映射类型（DB CHECK 只允许 5 个值）。"""
    return {
        "P": "product",
        "ALIAS": "product",
        "C": "partner",
        "S": "partner",
        "TF": "intent",
    }.get(slot_code, "product")


def _plain(value: Any) -> Any:
    """把值压成可 JSON 序列化的标量（Decimal/UUID 转字符串）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def summarize_corrections(rows: Sequence[CorrectionEvent]) -> dict[str, Any]:
    """纠错汇总（运维看「哪类槽位最常被改」）。"""
    counter: dict[str, int] = {}
    for row in rows:
        counter[str(row.slot_code)] = counter.get(str(row.slot_code), 0) + 1
    return {
        "total": len(rows),
        "by_slot": counter,
        "top_slot": max(counter, key=lambda key: counter[key]) if counter else None,
    }
