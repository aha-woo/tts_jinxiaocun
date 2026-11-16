"""音频链路：**100KiB 硬断言 + 类型层禁掉 base64 + voice_log 只存文本**（验收项④）。

为什么必须钉在这里
------------------
``callContainer`` 的请求体有 **文本 100KiB 硬限制**。音频只要进了请求体：
1. 一定超限（音频动辄几百 KB）；2. base64 还会再放大 33%；3. 超限报错信息通常与「音频」无关，
   排查成本极高。所以这条限制必须在**两个入口**同一时刻生效，且**同一个常量**。

三条断言（团队指定的原文口径）
------------------------------
1. ``/upload/ticket`` 与 ``/voice/*`` **共用同一个常量**，任一层超限都返回
   ``E_AUDIO_TOO_LARGE``(1404/413)，**不是两套阈值**；
2. 入参 Schema **根本不定义音频字段**：客户端传 ``audio_base64`` 直接 ValidationError
   （``extra="forbid"``），**类型层面**禁掉，而不是「文档说不要传」；
3. ``voice_log`` **只存 ``asr_text``**，表里没有、也不允许出现音频字节列。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect

from app.config import Settings
from app.models.voice import VoiceLog
from app.services.upload_ticket import (
    FORBIDDEN_AUDIO_KEYS,
    MAX_AUDIO_BYTES,
    AudioTooLargeError,
    UploadTicketRequest,
    UploadTicketService,
    enforce_audio_limit,
    reject_audio_payload,
)
from app.voice.asr_provider import AsrProvider, FakeAsrProvider, build_asr_provider


# --------------------------------------------------------------------------- 断言 1
def test_both_entrypoints_share_the_same_100kib_constant() -> None:
    """``/upload/ticket`` 与 ``/voice/*`` 用的是**同一个** 100KiB 常量。"""
    settings = Settings()
    assert MAX_AUDIO_BYTES == 100 * 1024
    assert settings.audio_max_bytes == MAX_AUDIO_BYTES, "配置与常量必须是同一个值，不能各写一份"
    assert settings.max_request_body_bytes == MAX_AUDIO_BYTES, "请求体上限与音频上限同口径"

    service = UploadTicketService()
    assert service.max_bytes == MAX_AUDIO_BYTES

    # 恰好 100KiB 放行；多 1 字节立刻炸
    assert enforce_audio_limit(MAX_AUDIO_BYTES) == MAX_AUDIO_BYTES
    with pytest.raises(AudioTooLargeError) as excinfo:
        enforce_audio_limit(MAX_AUDIO_BYTES + 1)
    assert excinfo.value.error_code == "E_AUDIO_TOO_LARGE"
    assert excinfo.value.http_status == 413

    # 票据签发时（/upload/ticket 路径）也当场拒，不等到上传完
    with pytest.raises(AudioTooLargeError):
        service.issue(
            UploadTicketRequest(client_bytes=MAX_AUDIO_BYTES + 1),
            tenant_id="tenant-a",
            member_id="member-1",
        )


# --------------------------------------------------------------------------- 断言 2
@pytest.mark.parametrize("field", ["audio", "audio_base64", "base64", "data", "file", "blob"])
def test_request_schema_has_no_audio_field(field: str) -> None:
    """入参 Schema **不定义**音频字段：传了就 422，类型层面禁掉 base64。"""
    with pytest.raises(ValidationError) as excinfo:
        UploadTicketRequest(**{field: "U2FsdGVkX1+..."})
    message = str(excinfo.value)
    assert "extra_forbidden" in message or "Extra inputs are not permitted" in message
    assert field in message


def test_forbidden_audio_keys_are_rejected_explicitly() -> None:
    """最后一道防线：绕过 Pydantic 的路径也会被显式拒。"""
    assert "audio_base64" in FORBIDDEN_AUDIO_KEYS
    reject_audio_payload({"duration_ms": 1200})  # 正常字段放行
    with pytest.raises(ValueError):
        reject_audio_payload({"duration_ms": 1200, "audio_base64": "AAAA"})


def test_asr_provider_signature_takes_only_a_reference() -> None:
    """ASR 协议签名**只有引用**，没有任何 ``bytes``/``base64`` 参数。"""
    params = inspect.signature(AsrProvider.transcribe).parameters
    names = set(params)
    assert "audio_object_ref" in names
    for forbidden in ("audio", "audio_bytes", "data", "payload", "base64", "file"):
        assert forbidden not in names, f"ASR 接口不应出现 {forbidden} 参数"


def test_fake_asr_provider_never_requires_bytes() -> None:
    """``FakeAsrProvider`` 只认 ref（未登记的 ref → 空文本 + 0 置信度，不猜）。"""
    provider = FakeAsrProvider({"voice/t/1/a.silk": ("今天进了两箱可乐", 0.94)})
    hit = provider.transcribe("voice/t/1/a.silk")
    assert hit.text == "今天进了两箱可乐" and hit.confidence > 0
    miss = provider.transcribe("voice/t/1/unknown.silk")
    assert miss.text == "" and miss.confidence == 0.0


# --------------------------------------------------------------------------- 断言 3
def test_voice_log_stores_text_only_and_has_no_audio_column() -> None:
    """``voice_log`` 只存 ``asr_text``：表里没有音频字节列（也禁止加）。"""
    columns = {column.name for column in sa_inspect(VoiceLog).columns}
    assert "asr_text" in columns
    for forbidden in ("audio", "audio_bytes", "audio_base64", "audio_blob", "payload_bytes", "raw_audio"):
        assert forbidden not in columns, f"voice_log 不得存音频字节（发现列 {forbidden}）"
    # 对象存储只留引用，且引用属于会话表（可清理）
    assert "audio_object_ref" not in columns, "引用应在 voice_session 上，便于按会话限期删除"


def test_upload_ticket_ttl_and_cleanup_are_short() -> None:
    """临时对象**短 TTL + 限期删除**（合规：需核实法务口径）。"""
    service = UploadTicketService(ttl_seconds=120, cleanup_grace_seconds=300)
    ticket = service.issue(
        UploadTicketRequest(duration_ms=1500), tenant_id="tenant-a", member_id="m1"
    )
    view = ticket.view()
    assert view.max_bytes == MAX_AUDIO_BYTES
    assert "服务端不接收音频字节" in view.note
    # 引用按租户打头（对象存储层也做隔离）
    assert view.audio_object_ref.startswith("voice/tenant-a/")
    assert view.expires_at < view.cleanup_deadline


def test_upload_ticket_reference_is_tenant_scoped() -> None:
    """引用跨租户不可用（前缀隔离，不泄露是否存在）。"""
    service = UploadTicketService()
    ticket = service.issue(
        UploadTicketRequest(duration_ms=1000), tenant_id="tenant-a", member_id="m1"
    )
    assert service.verify(ticket.audio_object_ref, tenant_id="tenant-a") is ticket
    with pytest.raises(PermissionError):
        service.verify(ticket.audio_object_ref, tenant_id="tenant-b")


def test_asr_provider_factory_defaults_to_fake_and_needs_credentials_for_real() -> None:
    """默认 fake（**永不联网**）；``real`` 缺凭证立刻炸。"""
    settings = Settings()
    provider = build_asr_provider(settings)
    assert isinstance(provider, FakeAsrProvider)
    with pytest.raises(ValueError):
        build_asr_provider(Settings(asr_provider="real"))
