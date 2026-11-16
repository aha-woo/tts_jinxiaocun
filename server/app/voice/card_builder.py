"""G6 确认卡片：**7 区块一个都不能少**（架构 §2.3 ⑥ / H10 / 验收①）。

为什么卡片要单独一个构造器
--------------------------
卡片是**写操作的唯一入口**（H1）。所有「省钱」的手段（规则层 0 LLM、词表缓存）
都必须收敛到同一张卡片上——「省钱不省确认」。因此：

- 卡片 7 区块与数据字段一一映射，缺一块就是实现了残缺的确认流程
  （:data:`BLOCK_KEYS` + :meth:`ConfirmCardView.blocks` 的键集合是硬约束）；
- ``source`` 角标、``computed`` 的计算式、``pending`` 的「不得静默填值」
  全部由 :mod:`app.voice.source_tags` 统一决定，不在各端各写一套；
- 主按钮文案**动态带关键值**（「确认入库 2 箱可乐 ¥70」），
  写死「确认」等于让用户在不知道后果的情况下点确定；
- 多商品在**卡片层**展开成多行（T03 的 ``ParsedItem`` 衔接点），
  不在契约里加 items 字段（18 槽位契约不扩）。

电脑版微信大屏（``resizable``）与手机端**同一份** 7 区块：窄屏可以纵向堆叠，
但**不许砍区块**；C2 的按钮兜底是最低要求（禁止超时自动执行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.config import ContractBundle, get_contracts
from app.domain.intent_registry import IntentRegistry, IntentSpec, default_registry
from app.utils.money import format_fen
from app.voice.conflict_detector import Conflict
from app.voice.impact_preview import PreviewBundle, PreviewItem
from app.voice.output_schema import ParseResult
from app.voice.source_tags import PENDING_PLACEHOLDER, display_value, tag_for, validate_sources

__all__ = [
    "BLOCK_KEYS",
    "CardAction",
    "CardLineView",
    "CardSlotView",
    "ConfirmCardBuilder",
    "ConfirmCardView",
    "CardBuildError",
    "RiskBanner",
    "primary_verb_of",
]

#: 卡片 7 区块（顺序即展示顺序，缺任一即视为实现残缺）
BLOCK_KEYS: tuple[str, ...] = (
    "original",
    "intent",
    "slots",
    "calc",
    "impact",
    "risk",
    "actions",
)

#: 各意图的主按钮动词（读自语义，不硬编码业务规则；缺省用契约 ``name_zh``）
_PRIMARY_VERBS: Mapping[str, str] = {
    "purchase.create": "确认入库",
    "purchase.create_multi": "确认入库",
    "sales.create": "确认卖出",
    "sales.create_multi": "确认卖出",
    "sales_return.create": "确认客户退货",
    "purchase_return.create": "确认退货给供应商",
    "stock.take": "确认盘点",
    "stock.take_multi": "确认盘点",
    "stock.loss": "确认报损",
    "price.update": "确认改价",
    "payment.receive": "确认收款",
    "payment.pay": "确认付款",
    "debt.writeoff": "确认抹零",
    "partner.create": "确认新建往来单位",
    "partner.update": "确认修改往来单位",
    "product.create": "确认新建商品",
    "product.update": "确认修改商品",
    "product.alias.add": "确认记住这个说法",
    "product.disable": "确认停用商品",
    "sys.teach": "确认记住这个说法",
    "sys.correct": "确认更正",
}


class CardBuildError(RuntimeError):
    """卡片构造失败（R0/R3 不该出卡片、区块缺失等）。"""


@dataclass(frozen=True)
class CardAction:
    """卡片 ⑦ 区的一个操作按钮。"""

    key: str
    label: str
    kind: str
    #: 是否需要二次语音（C2）；**始终**同时给按钮兜底
    requires_voice: bool = False
    button_fallback: bool = True
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "requires_voice": self.requires_voice,
            "button_fallback": self.button_fallback,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CardSlotView:
    """卡片 ③ 区的一行槽位。"""

    code: str
    label_zh: str
    value: Any
    display: str
    source: str
    tag: str | None
    tone: str
    editable: bool
    calc_trace: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "code": self.code,
            "label_zh": self.label_zh,
            "value": self.value,
            "display": self.display,
            "source": self.source,
            "tag": self.tag,
            "tone": self.tone,
            "editable": self.editable,
            "calc_trace": self.calc_trace,
        }


@dataclass(frozen=True)
class CardLineView:
    """多商品展开后的一行（``*_multi`` 出 N 行，不是只出第一行）。"""

    product_label: str
    quantity: Any
    unit: str
    amount_fen: int
    unit_price_fen: int | None = None
    qty_base: Any = None
    pending: bool = False
    source_map: Mapping[str, str] = field(default_factory=dict)
    calc_trace: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "product_label": self.product_label,
            "quantity": self.quantity,
            "unit": self.unit,
            "amount_fen": self.amount_fen,
            "amount_display": format_fen(self.amount_fen),
            "unit_price_fen": self.unit_price_fen,
            "qty_base": None if self.qty_base is None else str(self.qty_base),
            "pending": self.pending,
            "source_map": dict(self.source_map),
            "calc_trace": self.calc_trace,
        }


@dataclass(frozen=True)
class RiskBanner:
    """卡片 ⑥ 区风险条。"""

    level: str
    reason_zh: str
    conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "level": self.level,
            "reason_zh": self.reason_zh,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True)
class ConfirmCardView:
    """一张确认卡片的完整视图（7 区块 + 落账所需的元信息）。"""

    card_id: str
    session_id: str
    # ① 原文区
    asr_text: str
    asr_confidence: float
    low_confidence_highlight: bool
    # ② 意图区
    intent_id: str
    intent_label_zh: str
    domain: str
    domain_label_zh: str
    risk_level: str
    confirm_policy: str
    # ③~⑦
    slots: tuple[CardSlotView, ...]
    calc_traces: tuple[str, ...]
    impact_preview: tuple[PreviewItem, ...]
    risk_banner: RiskBanner
    primary_label: str
    actions: tuple[CardAction, ...]
    # 多商品行
    lines: tuple[CardLineView, ...] = ()
    # 落账元信息
    preview_version: int = 0
    expires_at: datetime | None = None
    route: str = "card"
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 7 区块
    def blocks(self) -> dict[str, Any]:
        """返回 **恰好 7 个**区块（键集合 == :data:`BLOCK_KEYS`）。"""
        return {
            "original": {
                "asr_text": self.asr_text,
                "asr_confidence": self.asr_confidence,
                "low_confidence_highlight": self.low_confidence_highlight,
            },
            "intent": {
                "intent_id": self.intent_id,
                "intent_label_zh": self.intent_label_zh,
                "domain": self.domain,
                "domain_label_zh": self.domain_label_zh,
                "risk_level": self.risk_level,
                "confirm_policy": self.confirm_policy,
            },
            "slots": [slot.as_dict() for slot in self.slots],
            "calc": {
                "calc_traces": list(self.calc_traces),
                "lines": [line.as_dict() for line in self.lines],
            },
            "impact": [item.as_dict() for item in self.impact_preview],
            "risk": self.risk_banner.as_dict(),
            "actions": {
                "primary_label": self.primary_label,
                "actions": [action.as_dict() for action in self.actions],
            },
        }

    def to_payload(self) -> dict[str, Any]:
        """API 响应体：7 区块 + 落账元信息。"""
        payload = self.blocks()
        payload["meta"] = {
            "card_id": self.card_id,
            "session_id": self.session_id,
            "preview_version": self.preview_version,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "route": self.route,
            "warnings": list(self.warnings),
        }
        return payload

    def assert_complete(self) -> None:
        """自检：7 区块齐全、source 标注合规。

        Raises:
            CardBuildError: 缺区块或 source 标注违规。
        """
        payload = self.blocks()
        missing = [key for key in BLOCK_KEYS if key not in payload]
        if missing:
            raise CardBuildError(f"卡片缺区块：{missing}")
        if len(payload) != len(BLOCK_KEYS):
            raise CardBuildError(
                f"卡片区块数应为 {len(BLOCK_KEYS)}，实际 {len(payload)}：{sorted(payload)}"
            )
        source_map = {slot.code: slot.source for slot in self.slots}
        values = {slot.code: slot.value for slot in self.slots}
        problems = validate_sources(source_map, slots=values, calc_traces=self.calc_traces)
        if problems:
            raise CardBuildError("source 标注违规：" + "；".join(problems))


def primary_verb_of(spec: IntentSpec) -> str:
    """主按钮动词（``确认入库`` / ``确认卖出`` …）；缺省用契约中文名。"""
    return _PRIMARY_VERBS.get(spec.intent_id, f"确认{spec.name_zh}")


class ConfirmCardBuilder:
    """从 ``ParseResult`` + 预演结果构造 7 区块卡片。"""

    def __init__(
        self,
        contracts: ContractBundle | None = None,
        registry: IntentRegistry | None = None,
    ) -> None:
        """
        Args:
            contracts: 共享契约（槽位中文名、业务域中文名）。
            registry: 意图注册表（risk_level / confirm_policy 的单一真相源）。
        """
        self._contracts = contracts or get_contracts()
        self._registry = registry or default_registry()
        self._slot_labels: dict[str, str] = {
            str(slot["code"]): str(slot.get("name") or slot["code"])
            for slot in self._contracts.slots
        }
        self._domain_labels: dict[str, str] = {
            str(domain["code"]): str(domain.get("name") or domain["code"])
            for domain in self._contracts.domains
        }

    # ------------------------------------------------------------------ 入口
    def build(
        self,
        parse: ParseResult,
        *,
        card_id: str,
        session_id: str,
        asr_text: str,
        asr_confidence: float,
        impact: PreviewBundle | None = None,
        lines: Sequence[Mapping[str, Any]] = (),
        conflicts: Sequence[Conflict] = (),
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> ConfirmCardView:
        """构造卡片。

        Args:
            parse: 已通过 Schema 校验的解析结果。
            card_id: 卡片 id。
            session_id: 语音会话 id（同一个 ``session_id`` 复活卡片不重复扣次）。
            asr_text: ASR 原文。
            asr_confidence: ASR 置信度。
            impact: 只读预演结果。
            lines: 多商品行（来自 ``TemplateOutcome.items`` / LLM 的多行输出）。
            conflicts: 冲突检测结果（进风险条）。
            expires_at: 卡片 30s TTL 截止时间。
            now: 注入当前时间。

        Returns:
            :class:`ConfirmCardView`。

        Raises:
            CardBuildError: R0 不该出卡片 / R3 不该出卡片 / 缺必填槽位。
        """
        spec = self._registry.require(parse.intent)
        del now  # 当前卡片不依赖当前时间；TTL 由调用方算好传入
        if spec.is_readonly:
            raise CardBuildError(
                f"{spec.intent_id} 是 R0 只读：不得构建确认卡片、不写库、不扣次（H2）"
            )
        if spec.whitelist_blocked:
            raise CardBuildError(
                f"{spec.intent_id} 是 R3 白名单动作：语音只生成 pending_task，"
                "不得构建可执行的确认卡片（H8）"
            )
        missing = [code for code in spec.required_slots if code not in parse.slots]
        if missing:
            raise CardBuildError(f"{spec.intent_id} 缺必填槽位 {missing}，不得出写卡")

        bundle = impact if impact is not None else PreviewBundle()
        slot_views = self._slot_views(parse, spec)
        line_views = self._line_views(parse, lines)
        banner = self._risk_banner(spec, parse, conflicts, bundle)
        primary = self._primary_label(spec, parse, line_views, bundle)

        card = ConfirmCardView(
            card_id=card_id,
            session_id=session_id,
            asr_text=asr_text,
            asr_confidence=float(asr_confidence),
            low_confidence_highlight=float(asr_confidence) < 0.85,
            intent_id=spec.intent_id,
            intent_label_zh=spec.name_zh,
            domain=spec.domain,
            domain_label_zh=self._domain_labels.get(spec.domain, spec.domain),
            risk_level=spec.risk_level,
            confirm_policy=spec.confirm_policy,
            slots=slot_views,
            calc_traces=tuple(parse.calc_traces),
            impact_preview=tuple(bundle.items),
            risk_banner=banner,
            primary_label=primary,
            actions=self._actions(spec, line_views, slot_views),
            lines=line_views,
            preview_version=bundle.preview_version,
            expires_at=expires_at,
            route="card",
            warnings=tuple(bundle.warnings),
        )
        card.assert_complete()
        return card

    # ------------------------------------------------------------------ ③ 槽位表
    def _slot_views(
        self, parse: ParseResult, spec: IntentSpec
    ) -> tuple[CardSlotView, ...]:
        """按契约槽位顺序输出，``source`` 决定角标与底色。"""
        ordered = [code for code in spec.all_slots if code in parse.slots]
        # 契约里没声明但解析结果里有的槽位也要展示（透明优先于整齐）
        ordered += [code for code in parse.slots if code not in ordered]
        views: list[CardSlotView] = []
        for code in ordered:
            value = parse.slots.get(code)
            source = parse.source.get(code, "asr")
            tag = tag_for(source)
            views.append(
                CardSlotView(
                    code=code,
                    label_zh=self._slot_labels.get(code, code),
                    value=None if tag.source == "pending" else value,
                    display=display_value(code, value, source),
                    source=tag.source,
                    tag=tag.label,
                    tone=tag.tone,
                    editable=code not in spec.required_slots or True,
                    calc_trace=self._trace_for(code, parse),
                )
            )
        return tuple(views)

    def _trace_for(self, code: str, parse: ParseResult) -> str | None:
        """挑一条与某槽位相关的计算式（``computed`` 必须能对上号）。"""
        if parse.source.get(code) != "computed":
            return None
        for trace in parse.calc_traces:
            if trace:
                return trace
        return None

    # ------------------------------------------------------------------ 多商品行
    def _line_views(
        self, parse: ParseResult, lines: Sequence[Mapping[str, Any]]
    ) -> tuple[CardLineView, ...]:
        """多商品展开：优先用传入的 ``lines``，没有就用单商品兜底成一行。"""
        views: list[CardLineView] = []
        for line in lines:
            product = str(line.get("product_label") or line.get("product_name") or "")
            pending = bool(line.get("pending")) or not product
            views.append(
                CardLineView(
                    product_label=product or PENDING_PLACEHOLDER,
                    quantity=line.get("quantity"),
                    unit=str(line.get("unit") or ""),
                    amount_fen=int(line.get("amount_fen") or 0),
                    unit_price_fen=(
                        None
                        if line.get("unit_price_fen") is None
                        else int(line["unit_price_fen"])
                    ),
                    qty_base=line.get("qty_base"),
                    pending=pending,
                    source_map=dict(line.get("source_map") or {}),
                    calc_trace=line.get("calc_trace"),
                )
            )
        if views:
            return tuple(views)

        product = parse.slots.get("P")
        if product is None:
            return ()
        return (
            CardLineView(
                product_label=str(product),
                quantity=parse.slots.get("Q"),
                unit=str(parse.slots.get("U") or ""),
                amount_fen=int(parse.slots.get("TA") or 0),
                unit_price_fen=(
                    None if parse.slots.get("UP") is None else int(parse.slots["UP"])
                ),
                pending=parse.source.get("P") == "pending",
                source_map=dict(parse.source),
                calc_trace=parse.calc_traces[0] if parse.calc_traces else None,
            ),
        )

    # ------------------------------------------------------------------ ⑥ 风险条
    def _risk_banner(
        self,
        spec: IntentSpec,
        parse: ParseResult,
        conflicts: Sequence[Conflict],
        bundle: PreviewBundle,
    ) -> RiskBanner:
        """风险条：冲突 > 预演警告 > 低置信 > 常规。"""
        blocking = [conflict for conflict in conflicts if conflict.blocked]
        if blocking:
            return RiskBanner(
                level="danger",
                reason_zh="；".join(conflict.message for conflict in blocking),
                conflicts=tuple(conflict.code for conflict in blocking),
            )
        if bundle.warnings:
            return RiskBanner(
                level="warn",
                reason_zh="；".join(bundle.warnings[:2]),
                conflicts=tuple(conflict.code for conflict in conflicts),
            )
        if parse.confidence < 0.6:
            return RiskBanner(
                level="warn",
                reason_zh="没太听清，请核对下面的值再确认",
                conflicts=tuple(conflict.code for conflict in conflicts),
            )
        level = "danger" if spec.risk_level in {"R2", "R3"} else "ok"
        reason = {
            "R1": "常规记账，确认后立即入账（10 分钟内可撤销）",
            "R2": "高风险动作：确认后请再核对一次",
            "R3": "该动作只能在电脑版微信大屏确认，语音不直写",
        }.get(spec.risk_level, "确认后入账")
        if spec.confirm_policy == "C2":
            reason += "；需再说一句「确认」，也可直接点下面的按钮"
        return RiskBanner(
            level=level,
            reason_zh=reason,
            conflicts=tuple(conflict.code for conflict in conflicts),
        )

    # ------------------------------------------------------------------ ⑦ 操作区
    def _primary_label(
        self,
        spec: IntentSpec,
        parse: ParseResult,
        lines: Sequence[CardLineView],
        bundle: PreviewBundle,
    ) -> str:
        """主按钮文案**动态带关键值**（写死「确认」等于让用户闭眼点）。"""
        verb = primary_verb_of(spec)
        if len(lines) > 1:
            total = sum(line.amount_fen for line in lines)
            return f"{verb} {len(lines)} 个商品 {format_fen(total)}"
        if lines:
            line = lines[0]
            qty = line.quantity if line.quantity is not None else parse.slots.get("Q")
            qty_text = f"{qty}{line.unit}" if qty is not None else ""
            amount = line.amount_fen
            amount_text = format_fen(amount) if amount else ""
            parts = [verb, qty_text, line.product_label, amount_text]
            label = " ".join(part for part in parts if part)
            return label.strip()
        total = int(parse.slots.get("AMT") or parse.slots.get("TA") or 0)
        return f"{verb} {format_fen(total)}" if total else verb

    def _actions(
        self,
        spec: IntentSpec,
        lines: Sequence[CardLineView],
        slots: Sequence[CardSlotView] = (),
    ) -> tuple[CardAction, ...]:
        """操作区：确认（C2 带二次语音 + 按钮兜底）/ 编辑 / 取消。

        只要还有**待确认**的东西（行级 ``pending`` 或槽位级 ``source=pending``），
        第一个按钮就必须是「先确认黄色项」——否则用户会直接点「确认无误」，
        把「没听清」当成「没问题」落账，这正是 ``pending`` 要防的事。
        """
        needs_voice = spec.confirm_policy == "C2"
        actions: list[CardAction] = [
            CardAction(
                key="confirm",
                label="确认无误",
                kind="primary",
                requires_voice=needs_voice,
                button_fallback=True,
            )
        ]
        if needs_voice:
            actions.append(
                CardAction(
                    key="confirm_voice2",
                    label="我再念一遍「确认」",
                    kind="secondary",
                    requires_voice=True,
                    button_fallback=True,
                )
            )
        actions.append(CardAction(key="edit", label="改一下", kind="secondary"))
        actions.append(CardAction(key="cancel", label="不是这句", kind="ghost"))
        has_pending = any(line.pending for line in lines) or any(
            slot.source == "pending" for slot in slots
        )
        if has_pending:
            actions.insert(
                0,
                CardAction(
                    key="resolve_pending",
                    label="先确认黄色项",
                    kind="primary",
                    payload={"reason": "存在待确认槽位，不得静默落账"},
                ),
            )
        return tuple(actions)


def summarize_pending(slots: Iterable[CardSlotView]) -> tuple[str, ...]:
    """列出所有待确认槽位代号（端上提示用）。"""
    return tuple(slot.code for slot in slots if slot.source == "pending")
