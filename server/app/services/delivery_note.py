"""拍进货单（M2）：图片多模态识别 + 货品自动匹配 + 合计数交叉校验 + 确认入账。

设计约束（架构 & 护栏）：
1. 识别过程（调用中国开源视觉模型 Qwen2.5-VL / GLM-4V）不占数据库事务；
2. 识别结果先落 ``attachment``（``doc_id=None``）并写审计 ``delivery_note.parse``，绝不跳过确认直记账；
3. 商户核对后确认入账时，通过 ``LedgerService.create_card`` + ``LedgerService.confirm`` 完成建卡与原子记账，
   随后将 ``DocHeader.source`` 置为 ``"photo"`` 并回填 ``Attachment.doc_id``；
4. 识别总额与手写合计不一致时给出 ``total_mismatch`` 与差额告警。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.db.session import Database
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.doc import DocHeader
from app.models.ops import Attachment
from app.models.voice import VoiceSession
from app.services.audit import AuditService
from app.services.ledger import LedgerService
from app.services.unit_upgrade import set_unit_ratio_and_maybe_upgrade
from app.utils.date_utils import today_in_tz
from app.voice.impact_preview import ImpactPreviewService, build_deltas
from app.voice.output_schema import ParseResult
from app.voice.phonetic import canonical_phonetic, fuzzy_score
from app.voice.unit_converter import standard_weight_ratio
from app.voice.vision_provider import ExtractedDeliveryNote, VisionProvider, build_vision_provider

__all__ = [
    "confirm_delivery_note",
    "list_delivery_notes",
    "parse_delivery_note",
]


def _normalize_name(text: str) -> str:
    return "".join(ch for ch in (text or "").strip().lower() if not ch.isspace())


def _match_product(
    raw_name: str,
    products: Sequence[Product],
    aliases: Sequence[ProductAlias],
) -> Product | None:
    """按正名 → 个人/租户别名 → 拼音近音匹配现有商品（不跨租户）。"""
    cleaned = (raw_name or "").strip()
    if not cleaned:
        return None
    norm = _normalize_name(cleaned)
    by_id = {p.product_id: p for p in products}

    for p in products:
        full_norm = _normalize_name(f"{p.name}{p.spec}")
        if _normalize_name(p.name) == norm or (full_norm and full_norm == norm):
            return p

    for alias in aliases:
        if _normalize_name(alias.alias_text) == norm and alias.product_id in by_id:
            return by_id[alias.product_id]

    target_phon = canonical_phonetic(cleaned)
    best: tuple[float, Product] | None = None
    for p in products:
        score = fuzzy_score(cleaned, p.name)
        if target_phon and canonical_phonetic(p.name) == target_phon:
            score = max(score, 0.90)
        if score >= 0.84 and (best is None or score > best[0]):
            best = (score, p)
    return best[1] if best is not None else None


def _lookup_ratio(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    product: Product,
    unit: str,
) -> Decimal | None:
    if not unit or unit == product.base_unit:
        return Decimal("1")
    std = standard_weight_ratio(unit, product.base_unit)
    if std is not None:
        return std
    conv = session.execute(
        select(UnitConversion)
        .where(
            UnitConversion.tenant_id == tenant_id,
            UnitConversion.product_id == product.product_id,
            UnitConversion.from_unit == unit,
            UnitConversion.to_unit == product.base_unit,
            UnitConversion.status == "approved",
        )
        .limit(1)
    ).scalar_one_or_none()
    if conv is not None:
        return Decimal(str(conv.ratio))
    return None


def _enrich_lines(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    extracted: ExtractedDeliveryNote,
) -> tuple[list[dict[str, Any]], int]:
    products = list(
        session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.status.in_(("active", "draft")),
            )
        ).scalars()
    )
    aliases = list(
        session.execute(
            select(ProductAlias).where(ProductAlias.tenant_id == tenant_id)
        ).scalars()
    )
    enriched: list[dict[str, Any]] = []
    computed_total_fen = 0

    for idx, line in enumerate(extracted.lines):
        matched = _match_product(line.product_name, products, aliases)
        ratio = _lookup_ratio(session, tenant_id=tenant_id, product=matched, unit=line.unit) if matched else Decimal("1")
        qty_dec = Decimal(str(line.quantity))
        qty_base = qty_dec * ratio if ratio is not None else qty_dec
        line_amt = line.amount_fen
        if line_amt is None and line.unit_price_fen is not None:
            line_amt = int(round(float(qty_dec) * int(line.unit_price_fen)))
        if line_amt is not None:
            computed_total_fen += int(line_amt)

        enriched.append(
            {
                "line_index": idx,
                "product_name": line.product_name,
                "unit": line.unit,
                "quantity": line.quantity,
                "unit_price_fen": line.unit_price_fen,
                "amount_fen": line_amt,
                "matched_product_id": str(matched.product_id) if matched else None,
                "matched_product_name": (
                    f"{matched.name} {matched.spec}".strip() if matched else None
                ),
                "matched_base_unit": matched.base_unit if matched else None,
                "is_new_product": matched is None,
                "needs_unit_ratio": matched is not None and ratio is None,
                "unit_ratio": str(ratio) if (matched is not None and ratio is not None and ratio != Decimal("1")) else None,
                "qty_base": str(qty_base),
            }
        )
    return enriched, computed_total_fen


def parse_delivery_note(
    *,
    database: Database,
    settings: Any,
    tenant_context: Any = None,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    image_base64: str | None = None,
    image_url: str | None = None,
    object_ref: str | None = None,
    hint_text: str | None = None,
    vision_provider: VisionProvider | None = None,
) -> dict[str, Any]:
    """调用多模态视觉模型提取进货单明细，并保存待确认附件草稿。"""
    if not any((image_base64, image_url, object_ref, hint_text)):
        raise AppError(
            "E_VALIDATION",
            message="请先拍照或选择进货单图片再识别",
        )
    if image_base64 and len(image_base64) > 95_000:
        raise AppError(
            "E_VALIDATION",
            message="图片 base64 过大，请压缩后再传（或改走对象存储 file_id）",
        )

    provider = vision_provider or build_vision_provider(settings)
    extracted = provider.extract(
        image_url=image_url,
        image_base64=image_base64,
        object_ref=object_ref,
        hint_text=hint_text,
    )
    if not extracted.lines:
        raise AppError(
            "E_VALIDATION",
            message="没能从这张图里识别出商品明细，请拍清楚一些或改用手动/语音录入",
        )

    attachment_ref = (object_ref or image_url or f"inline://{uuid.uuid4()}").strip()
    today_iso = today_in_tz("Asia/Shanghai")

    sess_ctx = (
        database.session(context=tenant_context)
        if tenant_context is not None
        else database.session(bypass_scope=True)
    )
    with sess_ctx as session:
        lines, computed_total_fen = _enrich_lines(session, tenant_id=tenant_id, extracted=extracted)
        stated = extracted.stated_total_fen
        total_diff_fen = (int(stated) - int(computed_total_fen)) if stated is not None else 0
        total_mismatch = stated is not None and abs(total_diff_fen) > 1

        warnings: list[str] = []
        if total_mismatch:
            warnings.append(
                f"单据手写合计 ¥{(stated or 0) / 100:.2f} 与逐行计算合计 ¥{computed_total_fen / 100:.2f} "
                f"相差 ¥{abs(total_diff_fen) / 100:.2f}，请核对后再入账"
            )
        new_count = sum(1 for row in lines if row["is_new_product"])
        if new_count > 0:
            warnings.append(f"其中 {new_count} 样新货不在档案里，确认入库时会自动建档")
        if any(row["needs_unit_ratio"] for row in lines):
            warnings.append("有商品的进货单位与现有库存单位不同，请补充换算关系（例如 1 箱 = 24 瓶）")

        draft_json = {
            "supplier_name": extracted.supplier_name,
            "doc_date": extracted.doc_date or today_iso,
            "payment_status": extracted.payment_status,
            "stated_total_fen": stated,
            "computed_total_fen": computed_total_fen,
            "total_mismatch": total_mismatch,
            "total_diff_fen": total_diff_fen,
            "warnings": warnings,
            "lines": lines,
            "notes": extracted.notes,
            "model": extracted.model,
        }
        attachment = Attachment(
            tenant_id=tenant_id,
            store_id=store_id,
            doc_id=None,
            kind="delivery_note",
            object_ref=attachment_ref,
            ocr_json=draft_json,
            created_by=member_id,
        )
        session.add(attachment)
        session.flush()

        AuditService(session).record(
            tenant_id=tenant_id,
            action="delivery_note.parse",
            entity="attachment",
            entity_id=str(attachment.attachment_id),
            member_id=member_id,
            detail={
                "line_count": len(lines),
                "new_products": new_count,
                "total_mismatch": total_mismatch,
                "model": extracted.model,
            },
        )
        return {
            "attachment_id": str(attachment.attachment_id),
            "object_ref": attachment.object_ref,
            **draft_json,
        }


def confirm_delivery_note(
    *,
    session: Session,
    database: Database,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    member_id: uuid.UUID,
    attachment_id: uuid.UUID,
    lines: Sequence[Mapping[str, Any]],
    supplier_name: str | None = None,
    doc_date: str | None = None,
    payment_status: str = "已结",
    stated_total_fen: int | None = None,
) -> dict[str, Any]:
    """核对进货单并经由 ``LedgerService`` 正式建卡确认入账（原单标记 source='photo'）。"""
    attachment = session.execute(
        select(Attachment)
        .where(
            Attachment.tenant_id == tenant_id,
            Attachment.store_id == store_id,
            Attachment.attachment_id == attachment_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if attachment is None:
        raise AppError("E_NOT_FOUND", message="进货单识别记录不存在或无权操作")
    if attachment.doc_id is not None:
        raise AppError(
            "E_DUPLICATE_REQUEST",
            message="这张进货单已经确认入账过了，请勿重复记账",
            data={"doc_id": str(attachment.doc_id)},
        )

    if not lines:
        raise AppError("E_VALIDATION", message="进货单至少保留 1 行有效商品")

    resolved_date_str = (doc_date or today_in_tz("Asia/Shanghai")).strip()
    try:
        biz_date = date.fromisoformat(resolved_date_str)
    except ValueError as exc:
        raise AppError("E_VALIDATION", message="单据日期格式必须为 YYYY-MM-DD") from exc

    ps = "未结" if (payment_status or "").strip() in ("未结", "赊账", "欠款") else "已结"
    supplier = (supplier_name or "").strip() or "待补"
    if ps == "未结" and supplier in ("", "待补", "待确认"):
        raise AppError("E_VALIDATION", message="赊账进货必须填写供应商名称，方便记应付往来")

    products = list(
        session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.status.in_(("active", "draft")),
            )
        ).scalars()
    )
    aliases = list(
        session.execute(
            select(ProductAlias).where(ProductAlias.tenant_id == tenant_id)
        ).scalars()
    )

    prepared_lines: list[dict[str, Any]] = []
    total_amount_fen = 0
    traces: list[str] = []

    for idx, raw in enumerate(lines):
        pname = str(raw.get("product_name") or "").strip()
        unit = str(raw.get("unit") or "").strip()
        if not pname or not unit:
            raise AppError("E_VALIDATION", message=f"第 {idx + 1} 行缺少商品名或单位")
        try:
            qty_dec = Decimal(str(raw.get("quantity") or 0))
        except Exception as exc:  # noqa: BLE001
            raise AppError("E_VALIDATION", message=f"第 {idx + 1} 行数量不合法") from exc
        if qty_dec <= 0:
            raise AppError("E_VALIDATION", message=f"第 {idx + 1} 行数量必须大于 0")

        up_raw = raw.get("unit_price_fen")
        amt_raw = raw.get("amount_fen")
        up_fen = int(up_raw) if up_raw is not None else None
        amt_fen = int(amt_raw) if amt_raw is not None else None
        if amt_fen is None and up_fen is not None:
            amt_fen = int((qty_dec * Decimal(up_fen)).quantize(Decimal("1")))
        elif up_fen is None and amt_fen is not None:
            up_fen = int((Decimal(amt_fen) / qty_dec).quantize(Decimal("1")))
        if up_fen is None or amt_fen is None or up_fen < 0 or amt_fen < 0:
            raise AppError("E_VALIDATION", message=f"第 {idx + 1} 行「{pname}」请补全单价或金额")

        matched: Product | None = None
        pid_raw = raw.get("product_id") or raw.get("matched_product_id")
        if pid_raw:
            try:
                pid_uuid = uuid.UUID(str(pid_raw))
                matched = next((p for p in products if p.product_id == pid_uuid), None)
            except ValueError:
                matched = None
        if matched is None:
            matched = _match_product(pname, products, aliases)

        qty_base = qty_dec
        if matched is not None:
            ratio = _lookup_ratio(session, tenant_id=tenant_id, product=matched, unit=unit)
            if ratio is None:
                raw_ratio = raw.get("unit_ratio")
                try:
                    ratio_dec = Decimal(str(raw_ratio)) if raw_ratio not in (None, "") else Decimal("0")
                except Exception:  # noqa: BLE001
                    ratio_dec = Decimal("0")
                if ratio_dec <= 0:
                    raise AppError(
                        "E_VALIDATION",
                        message=(
                            f"「{matched.name}」档案基本单位是「{matched.base_unit}」，"
                            f"与进货单位「{unit}」不同，请先填入 1 {unit} = 多少 {matched.base_unit}"
                        ),
                    )
                set_unit_ratio_and_maybe_upgrade(
                    session,
                    tenant_id=tenant_id,
                    store_id=store_id,
                    member_id=member_id,
                    product_id=matched.product_id,
                    from_unit=unit,
                    to_unit=matched.base_unit,
                    ratio=ratio_dec,
                )
                ratio = ratio_dec
            qty_base = qty_dec * ratio

        total_amount_fen += amt_fen
        traces.append(f"{pname}：{qty_dec} {unit} × ¥{up_fen / 100:.2f} = ¥{amt_fen / 100:.2f}")
        prepared_lines.append(
            {
                "product_id": str(matched.product_id) if matched is not None else None,
                "product_name": pname,
                "product_label": f"{matched.name} {matched.spec}".strip() if matched is not None else pname,
                "quantity": float(qty_dec) if qty_dec % 1 else int(qty_dec),
                "unit": unit,
                "qty_base": str(qty_base),
                "unit_price_fen": up_fen,
                "amount_fen": amt_fen,
                "payment_status": ps,
                "calc_trace": traces[-1],
                "pending": False,
            }
        )

    voice_session = VoiceSession(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        state="CARD_SHOWN",
        clarify_round=0,
        expires_at=datetime.now(timezone.utc),
    )
    session.add(voice_session)
    session.flush()

    slots: dict[str, Any] = {
        "P": prepared_lines[0]["product_name"],
        "Q": prepared_lines[0]["quantity"],
        "U": prepared_lines[0]["unit"],
        "UP": prepared_lines[0]["unit_price_fen"],
        "TA": total_amount_fen,
        "D": biz_date.isoformat(),
        "PS": ps,
        "W": "主仓",
        "S": supplier,
    }
    source: dict[str, str] = {
        "P": "asr",
        "Q": "asr",
        "U": "asr",
        "UP": "asr",
        "TA": "computed",
        "D": "asr" if doc_date else "default",
        "PS": "asr" if ps == "未结" else "default",
        "W": "default",
        "S": "asr" if supplier != "待补" else "pending",
    }
    parse = ParseResult(
        intent="purchase.create_multi",
        slots=slots,
        source=source,  # type: ignore[arg-type]
        confidence=0.98,
        calc_traces=traces,
        channel="template",
        route="card",
    )

    deltas = build_deltas(
        intent_id="purchase.create_multi",
        side_effect={"inventory_delta": "increase", "ar_ap_delta": "payable_increase"},
        lines=prepared_lines,
    )
    preview = ImpactPreviewService(database).preview(
        tenant_id=tenant_id,
        store_id=store_id,
        deltas=deltas,
        session=None if database.is_pg else session,
    )

    ledger = LedgerService(session)
    card_res = ledger.create_card(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        session_id=voice_session.session_id,
        parse=parse,
        asr_text=f"拍进货单入库：{supplier} 共 {len(prepared_lines)} 行",
        asr_confidence=1.0,
        lines=prepared_lines,
        preview=preview,
    )
    confirmed = ledger.confirm(
        tenant_id=tenant_id,
        store_id=store_id,
        member_id=member_id,
        card_id=uuid.UUID(card_res.card_id),
        confirm_type="button",
        button_fallback=True,
        biz_date=biz_date,
    )
    if not confirmed.confirmed or not confirmed.doc_id:
        raise AppError(
            confirmed.error_code or "E_VALIDATION",
            message=confirmed.detail or "进货单确认入账失败",
        )

    doc_uuid = uuid.UUID(confirmed.doc_id)
    doc = session.execute(
        select(DocHeader)
        .where(DocHeader.tenant_id == tenant_id, DocHeader.doc_id == doc_uuid)
        .limit(1)
    ).scalar_one_or_none()
    if doc is not None:
        doc.source = "photo"
    attachment.doc_id = doc_uuid
    session.flush()

    AuditService(session).record(
        tenant_id=tenant_id,
        action="delivery_note.confirm",
        entity="attachment",
        entity_id=str(attachment.attachment_id),
        member_id=member_id,
        detail={
            "doc_id": confirmed.doc_id,
            "doc_no": confirmed.doc_no,
            "line_count": len(prepared_lines),
            "total_amount_fen": total_amount_fen,
            "stated_total_fen": stated_total_fen,
            "payment_status": ps,
        },
    )
    return {
        "attachment_id": str(attachment.attachment_id),
        "doc_id": confirmed.doc_id,
        "doc_no": confirmed.doc_no,
        "total_amount_fen": total_amount_fen,
        "payment_status": ps,
        "source": "photo",
        "line_count": len(prepared_lines),
    }


def list_delivery_notes(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """查询最近拍进货单记录（含是否已关联入账单据）。"""
    rows = session.execute(
        select(Attachment)
        .where(
            Attachment.tenant_id == tenant_id,
            Attachment.store_id == store_id,
            Attachment.kind == "delivery_note",
        )
        .order_by(Attachment.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).scalars()
    result: list[dict[str, Any]] = []
    for att in rows:
        ocr = dict(att.ocr_json or {})
        result.append(
            {
                "attachment_id": str(att.attachment_id),
                "doc_id": str(att.doc_id) if att.doc_id else None,
                "object_ref": att.object_ref,
                "supplier_name": ocr.get("supplier_name"),
                "computed_total_fen": ocr.get("computed_total_fen"),
                "line_count": len(ocr.get("lines") or []),
                "created_at": att.created_at.isoformat() if att.created_at else None,
            }
        )
    return result
