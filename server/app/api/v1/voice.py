"""语音会话与解析：`POST /voice/sessions`、`POST /voice/sessions/{id}/parse`。

解析后的五条分支（由 ``ParseResult.route`` 决定，**不再二次判断**）
---------------------------------------------------------------
| route | 处理 | 写库 | 扣次 | 出卡片 |
|---|---|---|---|---|
| ``static`` | 本地话术直接返回 | 否 | 否 | 否 |
| ``answer`` | R0 只读，查完就返回 | 否 | 否 | **否**（H2） |
| ``clarify`` | 进澄清状态机（≤2 轮） | 否 | 否 | 否 |
| ``manual`` | 澄清用尽，切数字键盘 | 否 | 否 | 否 |
| ``pending_task`` | R3，只生成待办 | 是（pending_task） | 否 | 否 |
| ``card`` | 影响预演 → 卡片 + 扣 1 次 | 是 | **是** | 是 |

**音频本体绝不进本接口**：入参只有 ``audio_object_ref``（引用），
100KiB 断言由 ``RequestSizeGuardMiddleware`` 与 ``/upload/ticket`` 共同守住（D11）。
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, can_view_cost, ids_of, require_roles, runtime_for, WRITE_ROLES
from app.api.v1.cards import apply_card_updates
from app.common.envelope import ok
from app.common.errors import AppError
from app.config import Settings
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep, get_database
from app.domain.doc_service import DocService
from app.domain.report_service import ReportService
from app.models.catalog import Product
from app.models.doc import DocHeader, DocLine
from app.models.org import Member
from app.models.partner import Partner
from app.models.voice import ConfirmCard, VoiceSession
from app.services.degrade import DegradeController
from app.services.entitlement import EntitlementService
from app.services.ledger import LedgerService, StoredCardPayload
from app.services.novice import can_auto_post_tier_a, novice_status_of
from app.services.pending_task import PendingTaskService
from app.services.quota import QuotaService
from app.services.upload_ticket import UploadTicketRequest, UploadTicketService
from app.services.voice_runtime import LedgerLine, lines_of_result, shared_voice_state
from app.utils.date_utils import today_in_tz
from app.utils.unit import CANONICAL_UNITS
from app.voice.asr_provider import build_asr_provider
from app.voice.card_builder import CardBuildError
from app.voice.clarifier_templates import build_clarify, pick_slot_to_ask
from app.voice.gate import Gate
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.number_normalizer import parse_money_yuan, parse_quantity
from app.voice.output_schema import SOURCE_VALUES, ClarifyPrompt, ParseResult
from app.voice.pipeline import PipelineResult
from app.voice.routing import (
    ROUTE_ANSWER,
    ROUTE_PENDING_TASK,
    route_of_spec,
)
from app.voice.teach import TeachPlan, clarify_options, plan_teach

router = APIRouter(prefix="/voice", tags=["voice"])

#: 语音会话的初始状态（§5.3）
STATE_RECORDING = "RECORDING"
STATE_FILTERING = "FILTERING"
STATE_PARSING = "PARSING"
STATE_CLARIFYING = "CLARIFYING"
STATE_MANUAL = "MANUAL_ENTRY"
STATE_AWAIT_CONFIRM = "AWAIT_CONFIRM"
STATE_PENDING_TASK = "PENDING_TASK"
STATE_CLOSED_READ = "CLOSED_READ"
STATE_REJECTED = "REJECTED"
STATE_FAILED = "FAILED"

#: 走「反做原单」这条路的意图（确认时需要定位到一张真实单据）
_REVERSAL_INTENTS: frozenset[str] = frozenset({"sys.undo", "sys.correct"})


def _bump_round(current: Any) -> int:
    """澄清轮数 +1（``None`` 视为 0；不越过 ``max_rounds=2``）。"""
    try:
        value = int(current or 0)
    except (TypeError, ValueError):  # pragma: no cover - 脏数据兜底
        value = 0
    return min(value + 1, 2)


def _json_number(value: Any) -> Any:
    """``Decimal`` → JSON 友好的数字（整数保持整数，免得界面上出现 ``2.0000``）。"""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):  # pragma: no cover - 脏数据兜底
        return value
    return int(number) if number == number.to_integral_value() else float(number)


def _json_safe(value: Any) -> Any:
    """把任意槽位值洗成**能进 JSON 列**的样子（``Decimal`` 是头号杀手）。

    快照要写进 ``voice_log.parse_result``（JSON 列）。``Decimal`` 直接塞进去
    会 ``StatementError``（不是 JSON 可序列化），而槽位里恰恰最爱出现金额。
    """
    if isinstance(value, Decimal):
        return _json_number(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _reversal_lines(
    session: Any, *, tenant_id: uuid.UUID, target: Any, runtime: Any
) -> list[Any]:
    """撤销/更正卡片的明细：**来自原单**（这次要反做的就是它）。

    为什么不能沿用「本次解析出的明细」：``sys.undo`` / ``sys.correct`` 的槽位里
    没有商品和数量，硬凑只会得到一行 ``pending=True`` 的空明细 →
    卡片一整片黄、确认被行级 pending 守卫拦下（**又是一张建得出、点下去必失败的卡**）。
    反做卡的明细本来就该是「原单那几行」，原样展示，用户才知道自己要撤的是什么。
    """
    rows = DocService(session).lines_of(tenant_id=tenant_id, doc_id=target.doc_id)
    lines: list[Any] = []
    for row in rows:
        entry = runtime.products.lexicon.lookup(str(row.product_id))
        lines.append(
            LedgerLine(
                product_id=str(row.product_id),
                product_label=entry.display_name() if entry is not None else str(row.product_id)[:8],
                # 卡片载荷要落 JSON 列，``Decimal`` 直接塞会 500（不是 JSON 可序列化）
                quantity=_json_number(row.qty),
                unit=str(row.unit),
                qty_base=row.qty_base,
                unit_price_fen=int(row.unit_price_fen) if row.unit_price_fen is not None else None,
                amount_fen=int(row.amount_fen) if row.amount_fen is not None else 0,
                calc_trace=f"原单 {target.doc_no} 的明细（按原样反做）",
                pending=False,
            )
        )
    return lines


def _teach_clarify(plan: TeachPlan) -> ClarifyPrompt:
    """把「教说法」问不出结果的情况，翻成一次标准追问（2~3 个可点选项）。"""
    if plan.reason == "no_alias":
        return ClarifyPrompt(
            slot="ALIAS",
            question="要把哪句话记成一个商品？照这个说：「以后我说『快乐水』就是可乐」",
            options=clarify_options(plan.candidates, fallback="再教一次"),
        )
    if plan.reason == "no_target":
        return ClarifyPrompt(
            slot="P",
            question=f"「{plan.alias}」要记成哪个商品？",
            options=clarify_options(plan.candidates, fallback="先不教了"),
        )
    if plan.reason == "ambiguous":
        return ClarifyPrompt(
            slot="P",
            question=f"「{plan.alias}」记成哪一个？（它现在的说法能对上好几个商品）",
            options=clarify_options(plan.candidates, fallback="先不教了"),
        )
    return ClarifyPrompt(
        slot="P",
        question=f"没找到「{plan.target_text or ''}」这个商品；「{plan.alias}」要记成哪个？",
        options=clarify_options(plan.candidates, fallback="先建这个商品"),
    )


def build_card_after_clarify(
    *,
    session: Any,
    request: Request,
    settings: Settings,
    context: Any,
    ids: Any,
    session_id: uuid.UUID,
    parse: Any,
    asr_text: str,
    asr_confidence: float,
) -> Any | None:
    """澄清补齐槽位后**当场建卡**（返回 ``CardCreation``；建不出来返回 ``None``）。

    为什么必须在这里建卡
    ------------------
    ``route=clarify`` 分支**不建卡**（澄清前不该占一张卡、不该扣次），
    所以澄清完成的那一刻，**手上确实还没有卡**。不在这里补建的话，
    用户点完选项只会拿到一句 ``resolved``，什么都没有 —— 澄清等于白做，
    「说不全」这条支线整条断掉。

    补建的卡走**同一条** ``LedgerService.create_card``，
    所以「卡片生成成功才扣 1 次」的口径完全不变（澄清阶段不扣次）。

    Returns:
        ``CardCreation``；建不出来（``CardBuildError``）时返回 ``None``。
    """
    runtime = runtime_for(session, settings, context)
    spec = runtime.registry.require(parse.intent)
    lines = lines_of_result(parse, runtime=runtime, items=(), partner_id=None)
    side_effect = dict(spec.side_effect or {})
    deltas = build_deltas(
        intent_id=spec.intent_id,
        side_effect=side_effect,
        lines=[line.as_dict() for line in lines if line.product_id],
    )
    preview_service = ImpactPreviewService(get_database(request))
    bundle = preview_service.preview(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        deltas=deltas,
        session=None if preview_service.dialect != "sqlite" else session,
    )
    try:
        return LedgerService(
            session,
            card_ttl_seconds=settings.card_ttl_seconds,
        ).create_card(
            tenant_id=ids.tenant_id,
            store_id=ids.store_id,
            member_id=ids.member_id,
            session_id=session_id,
            parse=parse,
            asr_text=asr_text,
            asr_confidence=asr_confidence,
            lines=[line.as_dict() for line in lines],
            preview=bundle,
            conflicts=(),
        )
    except CardBuildError:
        # 还是建不出来（槽位没补齐、或结构性错误）→ 交给调用方切键盘补录
        return None


def _card_build_failure_response(
    *,
    session: Any,
    voice_session: Any,
    spec: Any,
    error: CardBuildError,
    base: dict[str, Any],
) -> JSONResponse:
    """建卡失败时的**结构化**响应（**绝不 500**）。

    分两条路，唯一的依据是 ``error.missing``（缺了哪些必填槽位）：

    - **缺必填槽位** → 缺的是**单个信息**（用户说「进了点可乐」没数量，
      模型只抽到 ``P``）→ **追问一句**再出卡。这是「说不全」的常态，不是故障。
    - **结构性错误**（缺区块 / ``source`` 标注违规）→ 追问用户也补不上
      → 平滑切**数字键盘补录**（``route=manual``）。

    两条路都**不扣次**：卡没建出来就没有消耗（计费口径是「卡片生成成功才扣 1 次」）。
    """
    missing = list(error.missing)
    if missing:
        slot = pick_slot_to_ask(missing, spec=spec)
        prompt = build_clarify(spec.intent_id, slot) if slot is not None else None
        if prompt is not None:
            voice_session.state = STATE_CLARIFYING
            voice_session.clarify_round = 0
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "clarify",
                        "clarify": prompt.model_dump(),
                        "max_rounds": 2,
                        "error_code": "E_LOW_CONFIDENCE",
                        "missing_slots": missing,
                        "credit_consumed": False,
                        "detail": "这句话没说全，先补一个信息再出卡；补完才扣次",
                    }
                ),
            )
    voice_session.state = STATE_MANUAL
    voice_session.error_code = "E_CLARIFY_LIMIT"
    session.flush()
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                **base,
                "route": "manual",
                "manual_entry_required": True,
                "error_code": "E_CLARIFY_LIMIT",
                "message_zh": "没太听清，切到键盘补一下，点两下就好。",
                "credit_consumed": False,
                "detail": f"这条没能建卡（{error}），已切键盘补录；不扣次",
            }
        ),
    )



class SessionCreateRequest(BaseModel):
    """创建语音会话的入参（**无音频字段**）。"""

    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(default=0, ge=0, le=60_000, description="录音时长（毫秒）")
    energy_rms: float = Field(default=0.0, ge=0.0, description="端侧能量（纯环境音剔除用）")
    vad_tail_ms: int = Field(default=300, ge=0, le=2_000, description="端点前留白")
    client_fingerprint: str = Field(default="", max_length=128)
    mode: str = Field(default="tap", description="tap / hold")
    source: Literal["voice", "manual"] = Field(
        default="voice",
        description="voice=按住说（要过录音门禁）；manual=手动记账（无音频，跳过门禁）",
    )
    last_trigger_ms: int | None = Field(default=None, description="上次触发时间戳（误触判定）")


class ParseRequest(BaseModel):
    """解析入参：**只接受 ``audio_object_ref`` 引用或已转好的文本**。"""

    model_config = ConfigDict(extra="forbid")

    audio_object_ref: str | None = Field(default=None, max_length=512)
    asr_text: str | None = Field(default=None, max_length=2_000)
    asr_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    duration_ms: int = Field(default=2_000, ge=0, le=60_000)
    partner_id: str | None = Field(default=None, description="明确指定的往来单位（可选）")
    active_card_id: str | None = Field(
        default=None,
        description="当前正打开的待确认卡 ID（短语补充如「三箱」「一箱35」直接更新此卡，0 LLM 调用）",
    )


_NEW_INTENT_MARKERS: tuple[str, ...] = (
    "进了",
    "进货",
    "卖了",
    "卖出",
    "盘点",
    "还剩",
    "退货",
    "撤销",
    "作废",
    "新建",
    "删掉",
    "报损",
    "收款",
    "付款",
)


def _parse_fragment_updates(
    text: str, stored: StoredCardPayload, runtime: Any
) -> dict[str, Any] | None:
    """短片段追述解析（如「三箱」「一箱35」「一共70」「老王」「赊账」），0 次 LLM 调用。"""
    cleaned = (text or "").strip("，,。.！？! ")
    if not cleaned or len(cleaned) > 16:
        return None
    if any(marker in cleaned for marker in _NEW_INTENT_MARKERS):
        return None

    updates: dict[str, Any] = {}
    if any(w in cleaned for w in ("欠着", "赊账", "挂账", "未付")):
        updates["PS"] = "未结"
        return updates
    if any(w in cleaned for w in ("已付", "付清", "现结")):
        updates["PS"] = "已结"
        return updates

    if any(w in cleaned for w in ("一共", "总共", "合计", "共")):
        money = parse_money_yuan(cleaned)
        if money.ok and money.value is not None:
            updates["TA"] = int(
                (money.value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            return updates

    per_unit_match = re.search(r"(?:每|一|单价)\s*([箱件瓶包袋斤公斤克两套打盒条])?\s*(?:要|卖|进)?\s*(.+)", cleaned)
    if per_unit_match and any(ch.isdigit() or ch in "一二两三四五六七八九十百" for ch in per_unit_match.group(2)):
        money = parse_money_yuan(per_unit_match.group(2))
        if money.ok and money.value is not None:
            updates["UP"] = int(
                (money.value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            if per_unit_match.group(1):
                updates["U"] = per_unit_match.group(1)
            return updates

    qty = parse_quantity(cleaned)
    if qty.ok and qty.value is not None:
        unit_hit = next((u for u in sorted(CANONICAL_UNITS, key=len, reverse=True) if u in cleaned), None)
        if unit_hit is not None:
            updates["Q"] = int(qty.value) if qty.value == qty.value.to_integral_value() else float(qty.value)
            updates["U"] = unit_hit
            return updates

    # 往来单位追述（如「老王」「是老王」）
    partner_text = re.sub(r"^(?:是|给|找)\s*", "", cleaned).strip()
    if partner_text:
        is_purchase = stored.intent_id.startswith("purchase")
        p_code = "S" if is_purchase else "C"
        resolver = runtime.suppliers if is_purchase else runtime.customers
        hit = resolver.resolve(partner_text)
        if hit is not None and hit.product is not None:
            updates[p_code] = hit.product.name
            return updates
        if cleaned.startswith("是") and len(partner_text) <= 8:
            updates[p_code] = partner_text
            return updates
    return None


class Confirm2Request(BaseModel):
    """C2 二次确认入参（语音或按钮兜底）。"""

    model_config = ConfigDict(extra="forbid")

    spoken_text: str | None = Field(default=None, max_length=200)
    button: bool = Field(default=False, description="是否点了按钮兜底")


def _today_iso() -> str:
    """业务日（按租户默认时区 Asia/Shanghai 取当天日期，避免 00:00–08:00 记成昨天）。"""
    return today_in_tz("Asia/Shanghai")


@router.post("/sessions", summary="创建语音会话（门禁 + 上传票据 + 令牌桶）")
def create_session(
    payload: SessionCreateRequest,
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """G0/G1 门禁 → 令牌桶 → 上传票据；**早于 ASR**。

    Returns:
        包络 ``data: {session_id, upload_ticket, audio_object_ref, quota, degrade_level, state}``
    """
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    # 手动记账没有音频（duration=0），走录音门禁必然被 E_GATE_REJECT 打回——那不是
    # 业务错误，是「门禁只该管录音」。所以按 source 分流：manual 跳过 G0/G1，
    # 但仍要过令牌桶与额度（否则手动就成了绕开限流的后门）。
    if payload.source != "manual":
        gate = Gate().check(
            payload.duration_ms,
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            last_trigger_ms=payload.last_trigger_ms,
            has_energy=payload.energy_rms > 0,
            mode=payload.mode,
        )
        if not gate.passed:
            raise AppError(
                gate.error_code or "E_GATE_REJECT",
                message="录音太短、太长或没检测到说话，请重新按一下说。",
                data={"reason": gate.reason, "duration_ms": payload.duration_ms},
            )

    quota = QuotaService.from_settings(settings)
    decision = quota.take(str(ids.tenant_id))
    # 判 `allowed`，**不判 `exhausted`**：`exhausted` 只认「日额度用尽」
    # （``reason == 'quota_exceeded'``），而分钟桶限流是 ``reason='rate_limited'``
    # → 用 exhausted 会把分钟限流整个吞掉（实测 35 次连打 35 次放行）。
    if not decision.allowed:
        if decision.reason == "rate_limited":
            # 说得太快：**不降级**（不是额度问题），只让用户缓一下，且不扣次
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        "session_id": None,
                        "state": STATE_REJECTED,
                        "error_code": decision.error_code or "E_QUOTA_EXCEEDED",
                        "reason": "rate_limited",
                        "limit_per_minute": decision.limit,
                        "message_zh": (
                            f"一分钟最多 {decision.limit} 次，缓一下再按；"
                            "这次没有扣额度"
                        ),
                        "manual_entry_required": True,
                    }
                ),
            )
        # 额度用尽：**降级不断服**，仍然给会话（手打也能记）。
        # 走 `usage_ratio=1.0` 而不是自造参数：额度用尽在 DegradePolicy 里对应 L3
        # （语音解析暂停、手动录单保留），这样「为什么降级」与策略文件是同一套口径。
        degrade = DegradeController().decide(usage_ratio=1.0)
        return JSONResponse(
            status_code=200,
            content=ok(
                {
                    "session_id": None,
                    "state": STATE_REJECTED,
                    "error_code": "E_QUOTA_EXCEEDED",
                    "degrade_level": degrade.level,
                    "message_zh": degrade.message,
                    "manual_entry_required": True,
                }
            ),
        )

    voice_session = VoiceSession(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        state=STATE_RECORDING,
        clarify_round=0,
    )
    session.add(voice_session)
    session.flush()

    ticket_service = UploadTicketService(
        max_bytes=settings.audio_max_bytes,
        ttl_seconds=settings.upload_ticket_ttl_seconds,
    )
    ticket = ticket_service.issue(
        UploadTicketRequest(
            duration_ms=payload.duration_ms,
            client_bytes=None,
            codec="silk",
        ),
        tenant_id=str(ids.tenant_id),
        member_id=str(ids.member_id),
        session_id=str(voice_session.session_id),
    )

    voice_session.audio_object_ref = ticket.audio_object_ref
    session.flush()

    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "session_id": str(voice_session.session_id),
                "state": STATE_RECORDING,
                "upload_ticket": {
                    "ticket_id": ticket.ticket_id,
                    "audio_object_ref": ticket.audio_object_ref,
                    "upload_prefix": ticket.upload_prefix,
                    "expires_at": ticket.expires_at.isoformat(),
                    "max_bytes": settings.audio_max_bytes,
                },
                "audio_object_ref": ticket.audio_object_ref,
                "quota": {"remaining_minute": decision.remaining, "scope": decision.scope},
                "degrade_level": "L0",
                "note": "音频请用 wx.cloud.uploadFile 直传对象存储，callContainer 只传引用",
            }
        ),
    )


_REPEAT_PURCHASE_RE = re.compile(
    r"(?:跟|和|照|像)?上次(?:进货|一样|那单)|再来一单|按上次进|重复上次进货"
)


def _try_repeat_purchase_card(
    *,
    session: Any,
    database: Any,
    ids: Any,
    voice_session: VoiceSession,
    text: str,
    asr_confidence: float,
    novice_info: dict[str, Any],
    show_cost: bool = True,
) -> JSONResponse | None:
    """命中「跟上次一样」快捷复购时，查找最近一笔进货单生成确认卡（0 LLM，绝不静默直接记账）。"""
    if not _REPEAT_PURCHASE_RE.search(text or ""):
        return None
    from sqlalchemy import select

    recent_docs = list(
        session.execute(
            select(DocHeader)
            .where(
                DocHeader.tenant_id == ids.tenant_id,
                DocHeader.store_id == ids.store_id,
                DocHeader.doc_type == "purchase",
                DocHeader.status == "confirmed",
                DocHeader.reverses_doc_id.is_(None),
            )
            .order_by(DocHeader.biz_date.desc(), DocHeader.created_at.desc())
            .limit(10)
        ).scalars()
    )
    if not recent_docs:
        return JSONResponse(
            status_code=200,
            content=ok(
                {
                    "session_id": str(voice_session.session_id),
                    "stage": "G3",
                    "reason": "repeat_purchase_not_found",
                    "route": "clarify",
                    "asr_text": text,
                    "asr_confidence": asr_confidence,
                    "corrections": 0,
                    "rule_hit": True,
                    "llm_calls": 0,
                    "novice": novice_info,
                    "clarify": {
                        "slot": "P",
                        "question": "店里还没有之前的进货单，请直接说进了什么货、多少数量。",
                        "options": ["按今天新进货说", "点手动记账", "去拍进货单"],
                    },
                    "max_rounds": 2,
                }
            ),
        )

    chosen_doc = recent_docs[0]
    chosen_lines: list[DocLine] = []
    products_by_id: dict[uuid.UUID, Product] = {}

    for doc in recent_docs:
        d_lines = list(
            session.execute(
                select(DocLine).where(
                    DocLine.tenant_id == ids.tenant_id, DocLine.doc_id == doc.doc_id
                )
            ).scalars()
        )
        if not d_lines:
            continue
        pids = [dl.product_id for dl in d_lines]
        p_map = {
            p.product_id: p
            for p in session.execute(
                select(Product).where(Product.tenant_id == ids.tenant_id, Product.product_id.in_(pids))
            ).scalars()
        }
        if any(p.name in text for p in p_map.values()):
            chosen_doc = doc
            chosen_lines = d_lines
            products_by_id = p_map
            break
        if not chosen_lines:
            chosen_doc = doc
            chosen_lines = d_lines
            products_by_id = p_map

    if not chosen_lines:
        return None

    supplier_name = "待补"
    if chosen_doc.partner_id is not None:
        partner = session.execute(
            select(Partner)
            .where(Partner.tenant_id == ids.tenant_id, Partner.partner_id == chosen_doc.partner_id)
            .limit(1)
        ).scalar_one_or_none()
        if partner is not None:
            supplier_name = str(partner.name)

    today_iso = _today_iso()
    card_lines: list[dict[str, Any]] = []
    traces: list[str] = [f"复用上次进货单 {chosen_doc.doc_no}（{chosen_doc.biz_date.isoformat()}）"]
    total_fen = 0
    for dl in chosen_lines:
        prod = products_by_id.get(dl.product_id)
        pname = f"{prod.name} {prod.spec}".strip() if prod is not None else "未知货品"
        amt = (int(dl.amount_fen) if dl.amount_fen is not None else 0) if show_cost else 0
        total_fen += amt
        qty_val = float(dl.qty) if Decimal(dl.qty) % 1 else int(Decimal(dl.qty))
        card_lines.append(
            {
                "product_id": str(dl.product_id),
                "product_name": prod.name if prod is not None else pname,
                "product_label": pname,
                "quantity": qty_val,
                "unit": str(dl.unit),
                "qty_base": str(dl.qty_base),
                "unit_price_fen": dl.unit_price_fen if show_cost else None,
                "amount_fen": dl.amount_fen if show_cost else None,
                "partner_id": str(chosen_doc.partner_id) if chosen_doc.partner_id else None,
                "payment_status": str(chosen_doc.payment_status or "已结"),
                "calc_trace": (
                    f"{pname} {qty_val}{dl.unit}（沿用上次数量与单价）"
                    if show_cost
                    else f"{pname} {qty_val}{dl.unit}（沿用上次数量，进价按权限隐藏）"
                ),
                "pending": False,
            }
        )

    intent_id = "purchase.create_multi" if len(card_lines) > 1 else "purchase.create"
    first = card_lines[0]
    slots: dict[str, Any] = {
        "P": first["product_name"],
        "Q": first["quantity"],
        "U": first["unit"],
        "D": today_iso,
        "PS": str(chosen_doc.payment_status or "已结"),
        "W": "主仓",
        "S": supplier_name,
    }
    if first["unit_price_fen"] is not None:
        slots["UP"] = first["unit_price_fen"]
    if total_fen > 0:
        slots["TA"] = total_fen
    source: dict[str, Any] = {k: "default" for k in slots}
    source["P"] = "asr"
    if supplier_name == "待补":
        source["S"] = "pending"

    parse = ParseResult(
        intent=intent_id,
        slots=slots,
        source=source,
        confidence=0.92,
        calc_traces=traces,
        channel="template",
        route="card",
    )
    deltas = build_deltas(
        intent_id=intent_id,
        side_effect={"inventory_delta": "increase", "ar_ap_delta": "payable_increase"},
        lines=card_lines,
    )
    preview = ImpactPreviewService(database).preview(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        deltas=deltas,
        session=None if database.is_pg else session,
    )
    created = LedgerService(session).create_card(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        session_id=voice_session.session_id,
        parse=parse,
        asr_text=text,
        asr_confidence=asr_confidence,
        lines=card_lines,
        preview=preview,
    )
    voice_session.state = STATE_AWAIT_CONFIRM
    session.flush()
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "session_id": str(voice_session.session_id),
                "stage": "G3",
                "reason": "repeat_purchase",
                "route": "card",
                "auto_posted": False,
                "repeat_from_doc_no": chosen_doc.doc_no,
                "asr_text": text,
                "asr_confidence": asr_confidence,
                "corrections": 0,
                "rule_hit": True,
                "llm_calls": 0,
                "parse_result": parse.model_dump(),
                "novice": novice_info,
                "card": created.as_dict(),
                "credit_consumed": created.billed,
            }
        ),
    )


@router.post("/sessions/{session_id}/parse", summary="解析（G2→G3→G4→G5→G6）")
def parse_session(
    session_id: uuid.UUID,
    payload: ParseRequest,
    request: Request,
    context: AuthContextDep,
    settings: SettingsDep,
) -> JSONResponse:
    """跑完整漏斗并按 ``route`` 分流（PG 下解析前释放只读 DB 会话，避免持连接等模型 HTTP）。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    database = get_database(request)

    text = (payload.asr_text or "").strip()
    asr_conf = float(payload.asr_confidence)
    if not text and (payload.audio_object_ref or "").strip():
        from sqlalchemy import select

        with database.session(context=context) as ro_sess:
            hotwords = [
                str(name)
                for name in ro_sess.execute(
                    select(Product.name)
                    .where(Product.tenant_id == ids.tenant_id, Product.status.in_(("active", "draft")))
                    .limit(50)
                ).scalars()
            ]
        asr_res = build_asr_provider(settings).transcribe(
            str(payload.audio_object_ref),
            duration_ms=payload.duration_ms,
            hotwords=hotwords,
        )
        text = (asr_res.text or "").strip()
        if asr_res.confidence > 0:
            asr_conf = float(asr_res.confidence)

    if not text:
        raise AppError(
            "E_VALIDATION",
            message="缺少 asr_text 且云端语音识别未返回有效文字",
            data={"audio_object_ref": payload.audio_object_ref},
        )

    today_iso = _today_iso()

    # ---- Phase A（短读事务）：校验会话与词表装载；出函数即释放 DB 连接 ----
    with database.session(context=context) as session:
        assert_writable(session, ids.tenant_id)
        voice_session = session.get(VoiceSession, session_id)
        if voice_session is None or voice_session.tenant_id != ids.tenant_id:
            raise AppError("E_NOT_FOUND", message="语音会话不存在或不属于当前租户")
        runtime = runtime_for(session, settings, context)

        # 「跟上次一样」快捷复购（0 LLM，出确认卡核对数量单价）
        member_row = session.get(Member, ids.member_id)
        show_cost = can_view_cost(session, context)
        repeat_resp = _try_repeat_purchase_card(
            session=session,
            database=database,
            ids=ids,
            voice_session=voice_session,
            text=text,
            asr_confidence=asr_conf,
            novice_info=novice_status_of(member_row),
            show_cost=show_cost,
        )
        if repeat_resp is not None:
            return repeat_resp

        # 片段追述：若前端传了当前活跃卡片 ID 且本句是短补充，直接走本地结构化更新（0 LLM）
        if payload.active_card_id:
            try:
                active_uuid = uuid.UUID(str(payload.active_card_id))
            except ValueError:
                active_uuid = None
            if active_uuid is not None:
                active_card = session.execute(
                    __import__("sqlalchemy")
                    .select(ConfirmCard)
                    .where(
                        ConfirmCard.tenant_id == ids.tenant_id,
                        ConfirmCard.card_id == active_uuid,
                        ConfirmCard.status == "active",
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if active_card is not None:
                    frag_updates = _parse_fragment_updates(
                        text, StoredCardPayload.from_json(active_card.slots), runtime
                    )
                    if frag_updates:
                        card_payload, feedback = apply_card_updates(
                            session=session,
                            request=request,
                            settings=settings,
                            context=context,
                            ids=ids,
                            card_id=active_uuid,
                            updates=frag_updates,
                        )
                        voice_session.state = STATE_AWAIT_CONFIRM
                        session.flush()
                        return JSONResponse(
                            status_code=200,
                            content=ok(
                                {
                                    "session_id": str(session_id),
                                    "stage": "G5",
                                    "reason": "fragment_updated",
                                    "route": "card_updated",
                                    "asr_text": text,
                                    "asr_confidence": payload.asr_confidence,
                                    "corrections": 0,
                                    "rule_hit": True,
                                    "llm_calls": 0,
                                    "card": card_payload,
                                    "parsed_updates": frag_updates,
                                    "credit_consumed": False,
                                    **(feedback or {}),
                                }
                            ),
                        )

    # ---- Phase B（无 DB 连接占用）：运行语音流水线 & 可能的大模型 HTTP ----
    result: PipelineResult = runtime.pipeline.run(
        text,
        today_iso=today_iso,
        duration_ms=payload.duration_ms,
        session_key=str(session_id),
        asr_confidence=payload.asr_confidence,
        resolver=runtime.products,
        customers=runtime.customers,
        suppliers=runtime.suppliers,
        tenant_id=str(ids.tenant_id),
        member_id=str(ids.member_id),
        dict_version=runtime.dict_version,
        personal_fixes=runtime.personal_fixes,
        skip_gate=True,
    )
    shared_state = shared_voice_state(settings)
    shared_state.cost_monitor.record_pipeline(result)

    # ---- Phase C（写事务）：回落解析快照、状态、卡片与直记 ----
    with database.session(context=context) as session:
        voice_session = session.get(VoiceSession, session_id)
        if voice_session is None or voice_session.tenant_id != ids.tenant_id:
            raise AppError("E_NOT_FOUND", message="语音会话不存在或不属于当前租户")
        if payload.audio_object_ref:
            voice_session.audio_object_ref = payload.audio_object_ref
        voice_session.state = STATE_PARSING
        session.flush()

        member = session.get(Member, ids.member_id)
        novice_info = novice_status_of(member)

        if result.parse is not None:
            LedgerService(session).record_parse(
                tenant_id=ids.tenant_id,
                store_id=ids.store_id,
                session_id=session_id,
                asr_text=result.asr_text or text,
                asr_confidence=result.asr_confidence,
                parse_channel=result.parse.channel or "template",
                parse_result={
                    "intent": result.parse.intent,
                    "slots": _json_safe(dict(result.parse.slots)),
                    "source": {str(k): str(v) for k, v in result.parse.source.items()},
                    "confidence": float(result.parse.confidence),
                    "calc_traces": list(result.parse.calc_traces),
                    "route": result.route,
                },
            )

        base = {
            "session_id": str(session_id),
            "stage": result.stage,
            "reason": result.reason,
            "asr_text": result.asr_text or text,
            "asr_confidence": result.asr_confidence,
            "corrections": result.corrections,
            "rule_hit": result.rule_hit,
            "llm_calls": result.llm_calls,
            "parse_result": result.parse.model_dump() if result.parse is not None else None,
            "trace": list(result.trace),
            "novice": novice_info,
        }

        if result.route == "static":
            voice_session.state = STATE_CLOSED_READ
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok({**base, "route": "static", "static_answer": result.static_answer}),
            )

        if result.error_code in {"E_NON_COMMAND", "E_FILTERED_DUP"}:
            voice_session.state = STATE_REJECTED
            voice_session.error_code = result.error_code
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok({**base, "route": "rejected", "error_code": result.error_code}),
            )

        if result.route == "clarify":
            voice_session.state = STATE_CLARIFYING
            voice_session.clarify_round = int(
                result.clarify_state.round_used if result.clarify_state is not None else 0
            )
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "clarify",
                        "clarify": (
                            result.parse.clarify.model_dump()
                            if result.parse is not None and result.parse.clarify is not None
                            else None
                        ),
                        "max_rounds": 2,
                    }
                ),
            )

        if result.route == "manual":
            voice_session.state = STATE_MANUAL
            voice_session.error_code = result.error_code
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "manual",
                        "manual_entry_required": True,
                        "message_zh": "没太听清，切到键盘补一下，点两下就好。",
                    }
                ),
            )

        spec = runtime.registry.require(result.parse.intent) if result.parse is not None else None
        if spec is not None and route_of_spec(spec) == ROUTE_ANSWER:
            show_cost = can_view_cost(session, context)
            answer = _read_only_answer(
                session,
                tenant_id=ids.tenant_id,
                store_id=ids.store_id,
                intent_id=spec.intent_id,
                parse=result.parse,
                runtime=runtime,
                show_cost=show_cost,
            )
            voice_session.state = STATE_CLOSED_READ
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok({**base, "route": "answer", "answer": answer}),
            )

        if result.parse is not None and spec is not None and route_of_spec(spec) == ROUTE_PENDING_TASK:
            task = PendingTaskService(session).create(
                tenant_id=ids.tenant_id,
                session_id=session_id,
                intent_id=spec.intent_id,
                payload={"slots": dict(result.parse.slots), "source": dict(result.parse.source)},
                member_id=ids.member_id,
            )
            voice_session.state = STATE_PENDING_TASK
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "pending_task",
                        "task": task.as_dict(),
                        "error_code": "E_R3_BLOCKED",
                        "hint_zh": "该动作只能在电脑版微信大屏管理页确认执行（语音不直写）",
                    }
                ),
            )

        reversal_target: Any = None
        if spec is not None and spec.intent_id in _REVERSAL_INTENTS and result.parse is not None:
            ledger = LedgerService(session, card_ttl_seconds=settings.card_ttl_seconds)
            reversal_target = ledger.resolve_reversal_target(
                tenant_id=ids.tenant_id, raw_ref=result.parse.slots.get("DOC")
            )
            if reversal_target is None:
                is_correct = spec.intent_id == "sys.correct"
                prompt = ClarifyPrompt(
                    slot="DOC",
                    question=(
                        "没找到要更正的原始单据（今天还没记过，或者那张已经撤销过了）。要更正哪一单？"
                        if is_correct
                        else "没找到可以撤销的单据（今天还没记过，或者已经撤销过了）。要撤销哪一单？"
                    ),
                    options=["我说个单号", "先记一单", "算了"],
                )
                voice_session.state = STATE_CLARIFYING
                voice_session.clarify_round = _bump_round(voice_session.clarify_round)
                session.flush()
                return JSONResponse(
                    status_code=200,
                    content=ok(
                        {
                            **base,
                            "route": "clarify",
                            "clarify": prompt.model_dump(),
                            "max_rounds": 2,
                        }
                    ),
                )

        if spec is not None and spec.intent_id == "sys.teach" and result.parse is not None:
            plan = plan_teach(text=text, resolver=runtime.products)
            if not plan.ok:
                prompt = _teach_clarify(plan)
                voice_session.state = STATE_CLARIFYING
                voice_session.clarify_round = _bump_round(voice_session.clarify_round)
                session.flush()
                return JSONResponse(
                    status_code=200,
                    content=ok(
                        {
                            **base,
                            "route": "clarify",
                            "clarify": prompt.model_dump(),
                            "max_rounds": 2,
                            "teach": {
                                "reason": plan.reason,
                                "alias": plan.alias,
                                "candidates": list(plan.candidates),
                            },
                        }
                    ),
                )
            assert plan.target is not None
            result.parse.intent = "product.alias.add"
            result.parse.slots = {"P": plan.target.product_name, "ALIAS": plan.alias}
            result.parse.source = {
                "P": "alias" if plan.target.from_alias else "asr",
                "ALIAS": "asr",
            }
            spec = runtime.registry.require("product.alias.add")
            base["parse_result"] = result.parse.model_dump()
            base["teach"] = {
                "original_intent": "sys.teach",
                "alias": plan.alias,
                "product": plan.target.product_name,
                "written_as": "product.alias.add",
                "note_zh": (
                    "教说法落成商品别名（同时进个人词表）："
                    "确认后下一句就能用这个说法"
                ),
            }
            session.flush()

        if result.parse is None:
            voice_session.state = STATE_FAILED
            voice_session.error_code = result.error_code or "E_LLM_SCHEMA_INVALID"
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "failed",
                        "error_code": result.error_code or "E_LLM_SCHEMA_INVALID",
                        "credit_consumed": False,
                        "detail": "解析失败不扣次，可重说或键盘补录",
                    }
                ),
            )

        items = result.template.items if result.template is not None else ()
        lines = (
            _reversal_lines(session, tenant_id=ids.tenant_id, target=reversal_target, runtime=runtime)
            if reversal_target is not None and spec.intent_id in _REVERSAL_INTENTS
            else lines_of_result(
                result.parse, runtime=runtime, items=items, partner_id=payload.partner_id
            )
        )
        side_effect = dict(spec.side_effect or {})
        deltas = build_deltas(
            intent_id=spec.intent_id,
            side_effect=side_effect,
            lines=[line.as_dict() for line in lines if line.product_id],
        )
        preview_service = ImpactPreviewService(database)
        bundle = preview_service.preview(
            tenant_id=ids.tenant_id,
            store_id=ids.store_id,
            deltas=deltas,
            session=None if preview_service.dialect != "sqlite" else session,
        )

        conflicts = tuple(result.template.conflicts) if result.template is not None else ()
        ledger_svc = LedgerService(session, card_ttl_seconds=settings.card_ttl_seconds)
        try:
            creation = ledger_svc.create_card(
                tenant_id=ids.tenant_id,
                store_id=ids.store_id,
                member_id=ids.member_id,
                session_id=session_id,
                parse=result.parse,
                asr_text=result.asr_text or text,
                asr_confidence=result.asr_confidence,
                lines=[line.as_dict() for line in lines],
                preview=bundle,
                conflicts=conflicts,
            )
        except CardBuildError as build_error:
            return _card_build_failure_response(
                session=session,
                voice_session=voice_session,
                spec=spec,
                error=build_error,
                base=base,
            )

        card_dict = creation.as_dict()
        card_gaps = list(card_dict.get("gaps") or ())
        if can_auto_post_tier_a(
            member,
            parse=result.parse,
            lines=lines,
            conflicts=conflicts,
            gaps=card_gaps,
            spec=spec,
            today_iso=today_iso,
        ):
            outcome = ledger_svc.confirm(
                tenant_id=ids.tenant_id,
                store_id=ids.store_id,
                member_id=ids.member_id,
                card_id=uuid.UUID(creation.card_id),
                confirm_type="button",
                biz_date=date.fromisoformat(today_iso),
            )
            voice_session.state = STATE_CLOSED_READ
            session.flush()
            return JSONResponse(
                status_code=200,
                content=ok(
                    {
                        **base,
                        "route": "auto_posted",
                        "auto_posted": True,
                        "card": card_dict,
                        "result": outcome.as_dict(),
                        "novice": outcome.novice or novice_status_of(member),
                        "undo_window_seconds": 5,
                        "credit_consumed": creation.billed,
                        "billing_note": "A 档高置信直记并已落账（5 秒内可点撤销）",
                    }
                ),
            )

        voice_session.state = STATE_AWAIT_CONFIRM
        session.flush()

        body: dict[str, Any] = {
            **base,
            "route": "card",
            "auto_posted": False,
            "card": card_dict,
            "credit_consumed": creation.billed,
            "billing_note": "卡片生成成功才扣 1 次；确认/取消/超时都不再扣",
        }
        if spec.intent_id == "sys.correct":
            body["next_step_zh"] = (
                "确认后我会先把这个单子反做掉（原单保留、可查）；"
                "然后你再重说一句正确的，比如「进了三箱可乐，一件三十五」。"
                "我不会替你猜新金额——猜错就是一笔你没确认过的账。"
            )

        return JSONResponse(status_code=200, content=ok(body))


def _read_only_answer(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    intent_id: str,
    parse: Any,
    runtime: Any,
    show_cost: bool = True,
) -> Any:
    """R0 只读：按意图查库并直接返回（**不建卡片**，严格按角色隐藏进价与毛利）。"""
    service = ReportService(session)
    if intent_id == "stock.query":
        product_ids = []
        product = runtime.products.resolve(str(parse.slots.get("P") or ""))
        if product is not None and product.product is not None:
            product_ids.append(uuid.UUID(product.product.product_id))
        items = []
        for row in service.stock_of(
            tenant_id=tenant_id, store_id=store_id, product_ids=product_ids
        ):
            item = row.as_dict()
            if not show_cost:
                item["avg_cost_fen"] = None
                item["last_purchase_price_fen"] = None
                item["cost_hidden"] = True
            else:
                item["cost_hidden"] = False
            items.append(item)
        return {"kind": "stock", "items": items, "cost_hidden": not show_cost}
    if intent_id == "stock.query_low":
        items = []
        for row in service.low_stock(tenant_id=tenant_id, store_id=store_id):
            item = row.as_dict()
            if not show_cost:
                item["avg_cost_fen"] = None
                item["last_purchase_price_fen"] = None
                item["cost_hidden"] = True
            else:
                item["cost_hidden"] = False
            items.append(item)
        return {"kind": "stock_low", "items": items, "cost_hidden": not show_cost}
    if intent_id in {"stock.query_movement"}:
        return {"kind": "movement", "items": []}
    if intent_id in {"doc.query", "doc.query_summary"}:
        items = []
        for row in service.docs(tenant_id=tenant_id, store_id=store_id):
            item = row.as_dict()
            if (not show_cost) and str(item.get("doc_type") or "") in ("purchase", "purchase_return"):
                item["total_amount_fen"] = None
                item["total_amount_display"] = "—"
                item["cost_hidden"] = True
            else:
                item["cost_hidden"] = False
            items.append(item)
        return {"kind": "docs", "items": items, "cost_hidden": not show_cost}
    if intent_id == "debt.query":
        return {
            "kind": "debt",
            "items": [row.as_dict() for row in service.debts(tenant_id=tenant_id)],
        }
    if intent_id in {"report.today", "report.period", "report.top"}:
        if intent_id == "report.top":
            return {
                "kind": "report_top",
                "items": list(service.top_products(tenant_id=tenant_id, store_id=store_id)),
            }
        report = service.today(
            tenant_id=tenant_id, store_id=store_id, biz_date=date.fromisoformat(_today_iso())
        ).as_dict()
        if not show_cost:
            report["purchase_fen"] = None
            report["purchase_display"] = None
            report["gross_profit_fen"] = None
            report["gross_profit_display"] = None
            report["cost_hidden"] = True
        else:
            report["cost_hidden"] = False
        return {"kind": "report", "report": report}
    return {"kind": "unknown", "note": f"{intent_id} 的只读查询尚未接线（需核实）"}


@router.post("/sessions/{session_id}/confirm2", summary="C2 二次确认（15s 超时不执行）")
def confirm2(
    session_id: uuid.UUID,
    payload: Confirm2Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """C2 二次语音确认；**必须**有按钮兜底，**禁止超时自动执行**。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    voice_session = session.get(VoiceSession, session_id)
    if voice_session is None or voice_session.tenant_id != ids.tenant_id:
        raise AppError("E_NOT_FOUND", message="语音会话不存在或不属于当前租户")

    ledger = LedgerService(session, card_ttl_seconds=settings.card_ttl_seconds)
    card = ledger._get_card_by_session(ids.tenant_id, session_id)  # noqa: SLF001 - 会话内复用
    if card is None:
        raise AppError("E_VALIDATION", message="该会话还没有卡片，请先解析出卡片")
    outcome = ledger.confirm(
        tenant_id=ids.tenant_id,
        store_id=ids.store_id,
        member_id=ids.member_id,
        card_id=card.card_id,
        confirm_type="voice2" if payload.spoken_text else "button",
        spoken_text=payload.spoken_text,
        button_fallback=payload.button,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "result": outcome.as_dict(),
                "note": "二次确认 15s 未响应就回收卡片，不会自动执行",
            }
        ),
    )


def source_values() -> tuple[str, ...]:
    """5 值 ``source`` 枚举（回执校验与文档对齐用）。"""
    return tuple(SOURCE_VALUES)
