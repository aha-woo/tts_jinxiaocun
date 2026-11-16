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

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1._shared import assert_writable, ids_of, require_roles, runtime_for, WRITE_ROLES
from app.common.envelope import ok
from app.common.errors import AppError
from app.config import Settings
from app.deps import AuthContextDep, SettingsDep, TenantSessionDep, get_database
from app.domain.report_service import ReportService
from app.models.voice import VoiceSession
from app.services.degrade import DegradeController
from app.services.entitlement import EntitlementService
from app.services.ledger import LedgerService
from app.services.pending_task import PendingTaskService
from app.services.quota import QuotaService
from app.services.upload_ticket import UploadTicketRequest, UploadTicketService
from app.services.voice_runtime import lines_of_result
from app.voice.gate import Gate
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.output_schema import SOURCE_VALUES
from app.voice.pipeline import PipelineResult
from app.voice.routing import (
    ROUTE_ANSWER,
    ROUTE_PENDING_TASK,
    route_of_spec,
)

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


class SessionCreateRequest(BaseModel):
    """创建语音会话的入参（**无音频字段**）。"""

    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(default=0, ge=0, le=60_000, description="录音时长（毫秒）")
    energy_rms: float = Field(default=0.0, ge=0.0, description="端侧能量（纯环境音剔除用）")
    vad_tail_ms: int = Field(default=300, ge=0, le=2_000, description="端点前留白")
    client_fingerprint: str = Field(default="", max_length=128)
    mode: str = Field(default="tap", description="tap / hold")
    last_trigger_ms: int | None = Field(default=None, description="上次触发时间戳（误触判定）")


class ParseRequest(BaseModel):
    """解析入参：**只接受 ``audio_object_ref`` 引用或已转好的文本**。"""

    model_config = ConfigDict(extra="forbid")

    audio_object_ref: str | None = Field(default=None, max_length=512)
    asr_text: str | None = Field(default=None, max_length=2_000)
    asr_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    duration_ms: int = Field(default=2_000, ge=0, le=60_000)
    partner_id: str | None = Field(default=None, description="明确指定的往来单位（可选）")


class Confirm2Request(BaseModel):
    """C2 二次确认入参（语音或按钮兜底）。"""

    model_config = ConfigDict(extra="forbid")

    spoken_text: str | None = Field(default=None, max_length=200)
    button: bool = Field(default=False, description="是否点了按钮兜底")


def _today_iso() -> str:
    """业务日（UTC；门店时区在 T05 端侧处理）。"""
    return datetime.now(timezone.utc).date().isoformat()


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
    if decision.exhausted:
        # 额度用尽：**降级不断服**，仍然给会话（手打也能记）
        degrade = DegradeController().decide(
            quota_exhausted=True, reason="token_bucket_exhausted"
        )
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
                "quota": {"remaining_minute": decision.remaining_minute, "scope": decision.scope},
                "degrade_level": "L0",
                "note": "音频请用 wx.cloud.uploadFile 直传对象存储，callContainer 只传引用",
            }
        ),
    )


@router.post("/sessions/{session_id}/parse", summary="解析（G2→G3→G4→G5→G6）")
def parse_session(
    session_id: uuid.UUID,
    payload: ParseRequest,
    request: Request,
    context: AuthContextDep,
    session: TenantSessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """跑完整漏斗并按 ``route`` 分流（唯一的写库/扣次入口在 ``card`` 分支）。

    Returns:
        包络 ``data: {stage, route, parse_result, ...}``
    """
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    voice_session = session.get(VoiceSession, session_id)
    if voice_session is None or voice_session.tenant_id != ids.tenant_id:
        raise AppError("E_NOT_FOUND", message="语音会话不存在或不属于当前租户")

    text = (payload.asr_text or "").strip()
    if not text:
        # 真实链路：凭 ``audio_object_ref`` 取临时音频送 ASR；本地无 ASR 时不伪造文本
        raise AppError(
            "E_VALIDATION",
            message="缺少 asr_text：真实链路会凭 audio_object_ref 取音频送 ASR（需核实 ASR 服务）",
            data={"audio_object_ref": payload.audio_object_ref},
        )
    if payload.audio_object_ref:
        voice_session.audio_object_ref = payload.audio_object_ref

    runtime = runtime_for(session, settings, context)
    voice_session.state = STATE_PARSING
    session.flush()

    result: PipelineResult = runtime.pipeline.run(
        text,
        today_iso=_today_iso(),
        duration_ms=payload.duration_ms,
        session_key=str(session_id),
        asr_confidence=payload.asr_confidence,
        resolver=runtime.products,
        customers=runtime.partners,
        suppliers=runtime.partners,
        tenant_id=str(ids.tenant_id),
        skip_gate=True,
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
    }

    # ---- ① 静态意图：本地话术，不进模型、不写库
    if result.route == "static":
        voice_session.state = STATE_CLOSED_READ
        session.flush()
        return JSONResponse(
            status_code=200,
            content=ok({**base, "route": "static", "static_answer": result.static_answer}),
        )

    # ---- ② G2 拦下（非命令 / 闲聊 / 60s 去重）：不扣次、不写库
    if result.error_code in {"E_NON_COMMAND", "E_FILTERED_DUP"}:
        voice_session.state = STATE_REJECTED
        voice_session.error_code = result.error_code
        session.flush()
        return JSONResponse(
            status_code=200,
            content=ok({**base, "route": "rejected", "error_code": result.error_code}),
        )

    # ---- ③ 澄清：进状态机，不扣次
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

    # ---- ④ 澄清用尽：切数字键盘补录（**不是** pending_task）
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

    # ---- ⑤ 只读（R0）：不建卡、不写库、不扣次
    # 路由判定统一走 app.voice.routing（测试与实现共用同一个判定点，见 test_route_matrix.py）
    spec = runtime.registry.require(result.parse.intent) if result.parse is not None else None
    if spec is not None and route_of_spec(spec) == ROUTE_ANSWER:
        answer = _read_only_answer(
            session,
            tenant_id=ids.tenant_id,
            store_id=ids.store_id,
            intent_id=spec.intent_id,
            parse=result.parse,
            runtime=runtime,
        )
        voice_session.state = STATE_CLOSED_READ
        session.flush()
        return JSONResponse(
            status_code=200,
            content=ok({**base, "route": "answer", "answer": answer}),
        )

    # ---- ⑥ R3：只生成待办，绝不直写
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

    # ---- ⑦ 出卡片：影响预演（只读）→ 建卡 → 同一事务扣 1 次
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
    lines = lines_of_result(
        result.parse, runtime=runtime, items=items, partner_id=payload.partner_id
    )
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

    conflicts = tuple(result.template.conflicts) if result.template is not None else ()
    creation = LedgerService(
        session,
        card_ttl_seconds=settings.card_ttl_seconds,
    ).create_card(
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
    voice_session.state = STATE_AWAIT_CONFIRM
    session.flush()

    return JSONResponse(
        status_code=200,
        content=ok(
            {
                **base,
                "route": "card",
                "card": creation.as_dict(),
                "credit_consumed": creation.billed,
                "billing_note": "卡片生成成功才扣 1 次；确认/取消/超时都不再扣",
            }
        ),
    )


def _read_only_answer(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    intent_id: str,
    parse: Any,
    runtime: Any,
) -> Any:
    """R0 只读：按意图查库并直接返回（**不建卡片**）。"""
    service = ReportService(session)
    if intent_id == "stock.query":
        product_ids = []
        product = runtime.products.resolve(str(parse.slots.get("P") or ""))
        if product is not None and product.product is not None:
            product_ids.append(uuid.UUID(product.product.product_id))
        return {
            "kind": "stock",
            "items": [
                row.as_dict()
                for row in service.stock_of(
                    tenant_id=tenant_id, store_id=store_id, product_ids=product_ids
                )
            ],
        }
    if intent_id == "stock.query_low":
        return {
            "kind": "stock_low",
            "items": [
                row.as_dict()
                for row in service.low_stock(tenant_id=tenant_id, store_id=store_id)
            ],
        }
    if intent_id in {"stock.query_movement"}:
        return {"kind": "movement", "items": []}
    if intent_id in {"doc.query", "doc.query_summary"}:
        return {
            "kind": "docs",
            "items": [row.as_dict() for row in service.docs(tenant_id=tenant_id, store_id=store_id)],
        }
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
        return {
            "kind": "report",
            "report": service.today(
                tenant_id=tenant_id, store_id=store_id, biz_date=date.today()
            ).as_dict(),
        }
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
