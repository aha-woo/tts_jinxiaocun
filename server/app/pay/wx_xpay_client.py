"""微信虚拟支付服务端直查客户端（``xpay/query_order``）。

凭证完备（``WX_APPID`` / ``WX_APP_SECRET`` / ``WX_PAY_APP_KEY``）时：
1. 调用 ``GET /cgi-bin/token`` 获取并缓存 ``access_token``；
2. 按微信虚拟支付接口签名规则计算 ``pay_sig = HMAC-SHA256(app_key, uri + "&" + body)``；
3. 调用 ``POST /xpay/query_order`` 查真实支付状态（status 2/3 表示已支付）。

凭证缺失时：安全返回 ``configured=False, paid=None``，绝不假装已支付。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "WxQueryOrderResult",
    "WxXpayClient",
    "compute_pay_sig",
]

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def compute_pay_sig(uri: str, body_json: str, app_key: str) -> str:
    """微信虚拟支付 ``pay_sig = HMAC-SHA256(appKey, uri + '&' + postBody)``。"""
    msg = f"{uri}&{body_json}".encode("utf-8")
    return hmac.new(app_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class WxQueryOrderResult:
    """``xpay/query_order`` 查单结论。"""

    configured: bool
    paid: bool | None
    raw_status: int | None = None
    detail: str = ""


class WxXpayClient:
    """微信虚拟支付服务端 API 客户端（带 token 内存缓存）。"""

    def __init__(
        self,
        *,
        appid: str,
        app_secret: str,
        pay_app_key: str,
        base_url: str = "https://api.weixin.qq.com",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._appid = (appid or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._pay_app_key = (pay_app_key or "").strip()
        self._base_url = (base_url or "https://api.weixin.qq.com").rstrip("/")
        self._timeout = float(timeout_seconds)

    @property
    def configured(self) -> bool:
        return bool(self._appid and self._app_secret and self._pay_app_key)

    def _get_access_token(self) -> str:
        cache_key = f"{self._base_url}:{self._appid}"
        cached = _TOKEN_CACHE.get(cache_key)
        now = time.time()
        if cached is not None and cached[1] > now + 30:
            return cached[0]

        url = f"{self._base_url}/cgi-bin/token"
        resp = httpx.get(
            url,
            params={
                "grant_type": "client_credential",
                "appid": self._appid,
                "secret": self._app_secret,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data.get('errmsg') or data}")
        expires_in = int(data.get("expires_in") or 7200)
        _TOKEN_CACHE[cache_key] = (token, now + max(60, expires_in - 60))
        return token

    def query_order(self, *, openid: str, out_trade_no: str, env: int = 0) -> WxQueryOrderResult:
        """调微信官方 ``/xpay/query_order`` 确认真实支付状态。"""
        if not self.configured:
            return WxQueryOrderResult(
                configured=False,
                paid=None,
                detail="未配置 WX_APPID / WX_APP_SECRET / WX_PAY_APP_KEY，无法直查微信 xpay/query_order",
            )
        if not (openid or "").strip():
            return WxQueryOrderResult(
                configured=True,
                paid=None,
                detail="缺少下单用户 openid，无法调微信 xpay/query_order",
            )

        uri = "/xpay/query_order"
        body: dict[str, Any] = {
            "openid": openid,
            "env": int(env),
            "order_id": out_trade_no,
        }
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        pay_sig = compute_pay_sig(uri, body_json, self._pay_app_key)
        try:
            token = self._get_access_token()
            resp = httpx.post(
                f"{self._base_url}{uri}",
                params={"access_token": token, "pay_sig": pay_sig},
                content=body_json.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return WxQueryOrderResult(
                configured=True,
                paid=None,
                detail=f"调用微信 xpay/query_order 失败: {exc}",
            )

        errcode = int(payload.get("errcode") or 0)
        if errcode != 0:
            return WxQueryOrderResult(
                configured=True,
                paid=None,
                detail=f"微信返回 errcode={errcode}: {payload.get('errmsg', '')}",
            )
        order_info = payload.get("order") or payload
        status = order_info.get("status")
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = -1
        # 微信虚拟支付状态约定：0=初始，1=待付款，2=已支付待发货，3=已发货完成，4/5=退款/关闭
        paid = status_int in (2, 3)
        return WxQueryOrderResult(
            configured=True,
            paid=paid,
            raw_status=status_int,
            detail=f"微信订单状态 status={status_int}",
        )
