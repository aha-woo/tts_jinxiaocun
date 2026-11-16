"""微信登录 provider 适配器（`wx.login` code -> openid）。

为什么做成可注入
----------------
真实 `jscode2session` 需要 AppSecret，本地/CI 没有也不该有。因此：
- ``FakeWxProvider``：本地与测试用，**禁止生产**（openid 由 code 稳定哈希得出）；
- ``RealWxProvider``：生产用，httpx 调 `jscode2session`；
- 由 ``Settings.wx_provider``（env ``WX_PROVIDER``）切换，默认 ``fake`` 以免误连现网。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.common.errors import AppError
from app.config import Settings


@dataclass(frozen=True)
class WxIdentity:
    """微信身份（不落库的 session_key 仅在内存中使用）。"""

    openid: str
    unionid: str | None = None
    session_key: str | None = None


class WxProvider(Protocol):
    """provider 协议（同步实现，路由用 `def` 让 FastAPI 放入线程池，避免阻塞事件循环）。"""

    def resolve_openid(self, code: str) -> WxIdentity:
        """用一次性 code 换取 openid。"""


class FakeWxProvider:
    """本地与测试专用 provider（**仅本地与测试，生产禁用**）。

    openid 由 code 的 sha256 前 24 位稳定生成：同一 code 多次调用得到同一 openid，
    便于测试「同一用户重复登录回到同一租户」。
    """

    def __init__(self, prefix: str = "fake-openid-") -> None:
        self._prefix = prefix

    def resolve_openid(self, code: str) -> WxIdentity:
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:24]
        return WxIdentity(openid=f"{self._prefix}{digest}", unionid=None, session_key=None)


class RealWxProvider:
    """生产 provider：调微信 `jscode2session`。

    失败统一抛 ``E_AUTH_INVALID``（不把微信原始错误码暴露给前端）。
    """

    def __init__(self, appid: str, secret: str, base_url: str, timeout_seconds: float = 5.0) -> None:
        if not appid or not secret:
            raise ValueError("RealWxProvider 需要 WX_APPID / WX_APP_SECRET")
        self._appid = appid
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def resolve_openid(self, code: str) -> WxIdentity:
        params = {
            "appid": self._appid,
            "secret": self._secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
        try:
            response = httpx.get(f"{self._base_url}/sns/jscode2session", params=params, timeout=self._timeout)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("E_AUTH_INVALID", message="微信登录服务暂不可用，请重试") from exc

        openid = payload.get("openid")
        if not openid:
            # errcode 40029=code 无效 / 45011=频率限制 / 40163=code 已用（需核实具体码值）
            raise AppError(
                "E_AUTH_INVALID",
                message="登录凭证无效或已过期，请重新进入小程序",
                data={"wx_errcode": payload.get("errcode")},
            )
        return WxIdentity(
            openid=str(openid),
            unionid=payload.get("unionid"),
            session_key=payload.get("session_key"),
        )


def build_wx_provider(settings: Settings) -> WxProvider:
    """按配置选择 provider；`real` 缺凭证时直接报错（fail-fast，不静默降级）。"""
    kind = settings.wx_provider.strip().lower()
    if kind == "fake":
        return FakeWxProvider()
    if kind == "real":
        return RealWxProvider(
            appid=settings.wx_appid,
            secret=settings.wx_app_secret,
            base_url=settings.wx_api_base_url,
            timeout_seconds=settings.wx_timeout_seconds,
        )
    raise ValueError(f"未知 WX_PROVIDER={settings.wx_provider!r}（只支持 fake / real）")
