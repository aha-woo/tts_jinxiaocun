"""ASR provider 适配器（可注入，**测试零联网**）。

为什么必须可注入
----------------
本机没有 ASR 服务、没有厂商 Key。若 provider 写死成真调用，任何解析链路测试都会变成
网络测试——既慢又不稳，还会掩盖真实 bug。因此：

- :class:`FakeAsrProvider`：**仅本地与测试，生产禁用**（按 ``audio_object_ref`` /
  预置文案查表返回，确定、不联网）；
- :class:`RealAsrProvider`：生产用，httpx 调厂商接口（**凭证缺失时直接抛错，不静默降级**）；
- 由 ``Settings.asr_provider``（env ``ASR_PROVIDER``）切换，默认 ``fake``。

**只接受引用**：``transcribe`` 的入参是 ``audio_object_ref``（对象存储临时 fileID），
本模块**没有任何接收音频字节 / base64 的参数**——从类型层面落实 D11
「音频本体不进 callContainer」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

import httpx

from app.common.errors import AppError
from app.config import Settings

__all__ = [
    "AsrResult",
    "AsrProvider",
    "FakeAsrProvider",
    "RealAsrProvider",
    "build_asr_provider",
]


@dataclass(frozen=True)
class AsrResult:
    """一次 ASR 结果。"""

    text: str
    confidence: float
    duration_ms: int = 0
    provider: str = "fake"
    audio_object_ref: str = ""


class AsrProvider(Protocol):
    """ASR 协议：**只吃引用，不吃音频本体**。"""

    def transcribe(self, audio_object_ref: str, *, duration_ms: int = 0) -> AsrResult:
        """把 ``audio_object_ref`` 换成文本。"""


class FakeAsrProvider:
    """本地与测试专用 ASR（**仅本地与测试，生产禁用**）。

    Args:
        fixtures: ``audio_object_ref -> (text, confidence)``；
            未登记的 ref 会返回空文本 + 0 置信度（让上层按「无有效语音」处理），
            而不是抛异常——避免测试里因为 fixture 漏配而掩盖真实断言。
        default_confidence: 未登记 ref 的置信度。
    """

    name = "fake"

    def __init__(
        self,
        fixtures: Mapping[str, tuple[str, float]] | None = None,
        default_confidence: float = 0.0,
    ) -> None:
        self._fixtures = dict(fixtures or {})
        self._default_confidence = default_confidence

    def transcribe(self, audio_object_ref: str, *, duration_ms: int = 0) -> AsrResult:
        """查表返回；未登记 -> 空文本（上层按 G2「无有效汉字」拒绝）。"""
        text, confidence = self._fixtures.get(audio_object_ref, ("", self._default_confidence))
        return AsrResult(
            text=text,
            confidence=float(confidence),
            duration_ms=int(duration_ms),
            provider=self.name,
            audio_object_ref=audio_object_ref,
        )


class RealAsrProvider:
    """生产 ASR provider（httpx 调厂商接口；超时/失败统一抛 ``E_INTERNAL``）。

    说明：具体厂商协议**需核实**，这里只固定「送引用、要 JSON、带超时」的形态；
    接真厂商时替换 ``_build_payload`` / ``_parse_payload`` 即可，不动调用方。
    """

    name = "real"

    def __init__(self, endpoint: str, api_key: str, timeout_seconds: float = 8.0) -> None:
        if not endpoint or not api_key:
            raise ValueError("RealAsrProvider 需要 ASR_ENDPOINT / ASR_API_KEY")
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout = float(timeout_seconds)

    def transcribe(self, audio_object_ref: str, *, duration_ms: int = 0) -> AsrResult:
        """送引用取文本；**不下载音频、不传字节**。"""
        payload = {"audio_object_ref": audio_object_ref, "duration_ms": int(duration_ms), "format": "json"}
        try:
            response = httpx.post(
                self._endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("E_INTERNAL", message="语音识别服务暂不可用，请重试") from exc

        text = str(body.get("text") or "").strip()
        confidence = float(body.get("confidence") or 0.0)
        return AsrResult(
            text=text,
            confidence=confidence,
            duration_ms=int(body.get("duration_ms") or duration_ms),
            provider=self.name,
            audio_object_ref=audio_object_ref,
        )


def build_asr_provider(settings: Settings, fixtures: Mapping[str, tuple[str, float]] | None = None) -> AsrProvider:
    """按配置选 provider；``real`` 缺凭证 fail-fast（不静默降级成 fake）。"""
    kind = (settings.asr_provider or "fake").strip().lower()
    if kind == "fake":
        return FakeAsrProvider(fixtures)
    if kind == "real":
        return RealAsrProvider(
            endpoint=settings.asr_endpoint,
            api_key=settings.asr_api_key,
            timeout_seconds=settings.asr_timeout_seconds,
        )
    raise ValueError(f"未知 ASR_PROVIDER={settings.asr_provider!r}（只支持 fake / real）")
