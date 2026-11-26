"""收摊经营总结与微信订阅消息推送（M2/V1 补齐）。"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.report_service import ReportService
from app.services.reconcile import reconcile_store
from app.utils.date_utils import today_in_tz

__all__ = [
    "build_closing_summary",
    "send_closing_summary",
]


def build_closing_summary(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    store_id: uuid.UUID,
    biz_date: date | None = None,
) -> dict[str, Any]:
    """汇总今日收摊快报：销售额、进货额、参考毛利、新欠款与三账体检状态。"""
    target_date = biz_date or date.fromisoformat(today_in_tz("Asia/Shanghai"))
    rep = ReportService(session).today(tenant_id=tenant_id, store_id=store_id, biz_date=target_date)
    recon = reconcile_store(session, tenant_id=tenant_id, store_id=store_id)
    profit_part = (
        f"毛利约 ¥{(rep.gross_profit_fen or 0) / 100:.2f}"
        if rep.gross_profit_fen is not None
        else "毛利待补进价"
    )
    summary_text = (
        f"{target_date.isoformat()} 收摊总结：共 {rep.doc_count} 笔，"
        f"卖了 ¥{rep.sales_fen / 100:.2f}，进货 ¥{rep.purchase_fen / 100:.2f}，{profit_part}"
    )
    if rep.missing_cost_count > 0:
        summary_text += f"（{rep.missing_cost_count} 单待补成本）"
    if not recon["ok"]:
        summary_text += "；三账核对发现差异，请抽空查看"

    return {
        "biz_date": target_date.isoformat(),
        "doc_count": rep.doc_count,
        "sales_fen": rep.sales_fen,
        "purchase_fen": rep.purchase_fen,
        "gross_profit_fen": rep.gross_profit_fen,
        "missing_cost_count": rep.missing_cost_count,
        "receivable_open_fen": rep.receivable_open_fen,
        "payable_open_fen": rep.payable_open_fen,
        "reconcile_ok": recon["ok"],
        "negative_count": recon["negative_count"],
        "summary_text": summary_text,
    }


def send_closing_summary(
    settings: Settings,
    *,
    openid: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """推送微信订阅消息；缺 WX_APPID / WX_APP_SECRET / WX_SUBSCRIBE_TEMPLATE_ID 时走 dry_run 安全返回。"""
    appid = (getattr(settings, "wx_appid", "") or "").strip()
    secret = (getattr(settings, "wx_app_secret", "") or "").strip()
    tmpl_id = (getattr(settings, "wx_subscribe_template_id", "") or "").strip()
    base_url = (getattr(settings, "wx_api_base_url", "https://api.weixin.qq.com") or "https://api.weixin.qq.com").rstrip("/")
    timeout = float(getattr(settings, "wx_timeout_seconds", 5.0) or 5.0)

    if not appid or not secret or not tmpl_id or not (openid or "").strip():
        return {
            "sent": False,
            "dry_run": True,
            "reason": "未配置 WX_APPID / WX_APP_SECRET / WX_SUBSCRIBE_TEMPLATE_ID（已返回本地总结内容）",
            "summary": summary,
        }

    try:
        tok_resp = httpx.get(
            f"{base_url}/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": appid, "secret": secret},
            timeout=timeout,
        )
        tok_resp.raise_for_status()
        access_token = str(tok_resp.json().get("access_token") or "").strip()
        if not access_token:
            return {
                "sent": False,
                "dry_run": False,
                "reason": f"获取 access_token 失败: {tok_resp.json()}",
                "summary": summary,
            }

        send_resp = httpx.post(
            f"{base_url}/cgi-bin/message/subscribe/send",
            params={"access_token": access_token},
            json={
                "touser": openid,
                "template_id": tmpl_id,
                "page": "pages/index/index",
                "data": {
                    "thing1": {"value": str(summary.get("biz_date") or "")[:20]},
                    "amount2": {"value": f"¥{int(summary.get('sales_fen') or 0) / 100:.2f}"},
                    "thing3": {"value": str(summary.get("summary_text") or "")[:20]},
                },
            },
            timeout=timeout,
        )
        send_resp.raise_for_status()
        body = send_resp.json()
        ok_send = int(body.get("errcode") or 0) == 0
        return {
            "sent": ok_send,
            "dry_run": False,
            "wx_response": body,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "sent": False,
            "dry_run": False,
            "reason": f"订阅消息推送异常: {exc}",
            "summary": summary,
        }
