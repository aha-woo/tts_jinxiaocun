"""音频上传票据：端侧直传对象存储，服务端**只收 ``audio_object_ref``**。

为什么这么绕（D11 的直接后果）
------------------------------
微信云托管的 ``callContainer`` 请求体有 **文本 100KiB 硬限制**：
音频 base64 进来必然超限，而且 base64 会把体积再放大 33%。
所以音频只能走 **端侧 ``wx.cloud.uploadFile`` 直传**，服务端只拿一个**引用字符串**。

三层防线（缺一不可）
--------------------
1. **类型层**：:class:`UploadTicketRequest` / ``VoiceParseRequest`` 里**根本没有音频字段**，
   ``extra="forbid"`` 让客户端传 ``audio_base64`` 直接 422 —— 从类型上禁掉，
   而不是「文档里说不要传」；
2. **入口层**：``/upload/ticket`` 与 ``/voice/*`` **共用同一个常量**
   :data:`MAX_AUDIO_BYTES`（100KiB，来自 ``settings.audio_max_bytes``），
   任何一层超限都返回 ``E_AUDIO_TOO_LARGE``(1404)；
3. **存储层**：票据带**短 TTL**，对象前缀按租户隔离（``voice/{tenant_id}/…``），
   过期即删（**合规要求：限期删除，需核实法务口径**）。

本模块**不接收字节**：没有任何参数是 ``bytes``，从签名上就传不进来。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_AUDIO_BYTES",
    "AudioTooLargeError",
    "UploadTicket",
    "UploadTicketRequest",
    "UploadTicketService",
    "UploadTicketView",
    "enforce_audio_limit",
    "reject_audio_payload",
]

#: 音频上限：**与 callContainer 文本体上限同一口径**（100 KiB）
MAX_AUDIO_BYTES: int = 100 * 1024

#: 音频字段黑名单：出现即视为客户端用错通道（类型层已禁，这里是最后一道）
FORBIDDEN_AUDIO_KEYS: frozenset[str] = frozenset(
    {
        "audio",
        "audio_base64",
        "audio_bytes",
        "audiobase64",
        "base64",
        "file",
        "file_bytes",
        "blob",
        "data",
    }
)


class AudioTooLargeError(ValueError):
    """音频/请求体超过 100KiB（映射 ``E_AUDIO_TOO_LARGE``，413）。"""

    error_code = "E_AUDIO_TOO_LARGE"
    http_status = 413

    def __init__(self, actual: int, limit: int = MAX_AUDIO_BYTES) -> None:
        self.actual = actual
        self.limit = limit
        super().__init__(
            f"音频/请求体 {actual} 字节超过上限 {limit} 字节（100KiB）："
            "请改用端侧 wx.cloud.uploadFile 直传，只回传 audio_object_ref"
        )


def enforce_audio_limit(size_bytes: int, *, limit: int = MAX_AUDIO_BYTES) -> int:
    """断言体积不超限（``/upload/ticket`` 与 ``/voice/*`` **共用**）。

    Args:
        size_bytes: 实际字节数（客户端上报或请求体长度）。
        limit: 上限，默认 :data:`MAX_AUDIO_BYTES`。

    Returns:
        原样返回 ``size_bytes``（便于链式调用）。

    Raises:
        AudioTooLargeError: 超限。
    """
    if size_bytes < 0:
        raise ValueError(f"体积不能为负：{size_bytes}")
    if size_bytes > limit:
        raise AudioTooLargeError(size_bytes, limit)
    return size_bytes


class UploadTicketRequest(BaseModel):
    """申请上传票据的入参 —— **不含任何音频字段**（``extra="forbid"`` 硬拒）。"""

    model_config = ConfigDict(extra="forbid")

    #: 预计时长（毫秒）：服务端据此估算体积与是否可在 G1 直接判超时
    duration_ms: int = Field(default=0, ge=0, le=60_000)
    #: 端侧预先算好的字节数（可选）；给了就**当场**断言 100KiB
    client_bytes: int | None = Field(default=None, ge=0)
    #: 业务门店（多门店时显式指定；不传则由会话上下文决定）
    store_id: uuid.UUID | None = None
    #: 音频编码（仅登记用途，不参与解析）
    codec: str = Field(default="silk", max_length=16)


class UploadTicketView(BaseModel):
    """票据出参（前端拿它去 ``wx.cloud.uploadFile``）。"""

    ticket_id: str
    #: 对象存储引用：**唯一**能进入 ``/voice/parse`` 的音频凭据
    audio_object_ref: str
    #: 上传前缀（端侧只允许往这个前缀写）
    upload_prefix: str
    expires_at: str
    max_bytes: int
    #: 过期后服务端自动删除（合规：限期删除，需核实）
    cleanup_deadline: str
    note: str = "上传后请只回传 audio_object_ref；服务端不接收音频字节"


@dataclass
class UploadTicket:
    """票据实体（短 TTL）。"""

    ticket_id: str
    tenant_id: str
    member_id: str
    store_id: str | None
    audio_object_ref: str
    upload_prefix: str
    expires_at: datetime
    cleanup_deadline: datetime
    max_bytes: int = MAX_AUDIO_BYTES
    codec: str = "silk"
    used: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """是否已过期（过期即不可用于解析）。"""
        return datetime.now(timezone.utc) >= self.expires_at

    def view(self) -> UploadTicketView:
        """转成对外视图。"""
        return UploadTicketView(
            ticket_id=self.ticket_id,
            audio_object_ref=self.audio_object_ref,
            upload_prefix=self.upload_prefix,
            expires_at=self.expires_at.isoformat(timespec="seconds"),
            max_bytes=self.max_bytes,
            cleanup_deadline=self.cleanup_deadline.isoformat(timespec="seconds"),
        )


class UploadTicketService:
    """票据签发与校验（**只处理引用，不处理字节**）。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_bytes: int = MAX_AUDIO_BYTES,
        cleanup_grace_seconds: int = 3600,
        bucket: str = "voice",
    ) -> None:
        """
        Args:
            ttl_seconds: 票据有效期（短 TTL）。
            max_bytes: 体积上限（与 ``settings.audio_max_bytes`` 同一个值）。
            cleanup_grace_seconds: 过期后多久强制删除（合规宽限期，**需核实**）。
            bucket: 对象存储桶/前缀根。
        """
        self._ttl_seconds = max(30, ttl_seconds)
        self._max_bytes = max_bytes
        self._cleanup_grace_seconds = max(60, cleanup_grace_seconds)
        self._bucket = bucket.strip("/") or "voice"
        self._issued: dict[str, UploadTicket] = {}

    # ------------------------------------------------------------------ 签发
    def issue(
        self,
        request: UploadTicketRequest,
        *,
        tenant_id: str,
        member_id: str,
        session_id: str | None = None,
    ) -> UploadTicket:
        """签发一张上传票据。

        Args:
            request: 入参（**无音频字段**）。
            tenant_id: 租户 id（对象前缀隔离的第一段）。
            member_id: 成员 id。
            session_id: 语音会话 id（可选；给了就按会话归档）。

        Returns:
            :class:`UploadTicket`

        Raises:
            AudioTooLargeError: ``client_bytes`` 已超 100KiB（**当场拒**，不等到上传完）。
        """
        if request.client_bytes is not None:
            enforce_audio_limit(request.client_bytes, limit=self._max_bytes)

        now = datetime.now(timezone.utc)
        ticket_id = uuid.uuid4().hex
        segment = session_id or uuid.uuid4().hex
        # 前缀按租户打头：对象存储层也做租户隔离（九道防线之一）
        prefix = f"{self._bucket}/{tenant_id}/{segment}"
        ref = f"{prefix}/{ticket_id}.{request.codec or 'silk'}"
        ticket = UploadTicket(
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            member_id=member_id,
            store_id=str(request.store_id) if request.store_id else None,
            audio_object_ref=ref,
            upload_prefix=prefix,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            cleanup_deadline=now + timedelta(seconds=self._ttl_seconds + self._cleanup_grace_seconds),
            max_bytes=self._max_bytes,
            codec=request.codec or "silk",
            meta={"duration_ms": request.duration_ms},
        )
        self._issued[ticket_id] = ticket
        return ticket

    # ------------------------------------------------------------------ 校验
    def verify(self, audio_object_ref: str, *, tenant_id: str) -> UploadTicket:
        """校验引用可用（存在、未过期、前缀属本租户）。

        Args:
            audio_object_ref: 端侧回传的引用。
            tenant_id: 当前租户。

        Returns:
            :class:`UploadTicket`

        Raises:
            PermissionError: 引用不属于本租户（跨租户越权）。
            LookupError: 引用不存在。
            TimeoutError: 票据已过期。
        """
        ref = (audio_object_ref or "").strip()
        if not ref:
            raise LookupError("audio_object_ref 为空")
        if f"/{tenant_id}/" not in f"/{ref}":
            # 前缀隔离：别的租户的引用一律不认（不泄露是否存在）
            raise PermissionError("audio_object_ref 不属于当前租户")
        ticket = next(
            (item for item in self._issued.values() if item.audio_object_ref == ref),
            None,
        )
        if ticket is None:
            raise LookupError("audio_object_ref 不存在或已清理")
        if ticket.expired:
            raise TimeoutError("上传票据已过期，请重新申请")
        return ticket

    def consume(self, ticket: UploadTicket) -> None:
        """标记票据已用于解析（同一票据解析两次会重新申请，避免重放）。"""
        ticket.used = True

    def cleanup(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """清理过期票据，返回被清理的 ``ticket_id``（限期删除，**需核实合规**）。"""
        moment = now or datetime.now(timezone.utc)
        doomed = [key for key, item in self._issued.items() if moment >= item.cleanup_deadline]
        for key in doomed:
            self._issued.pop(key, None)
        return tuple(doomed)

    # ------------------------------------------------------------------ 查询
    @property
    def max_bytes(self) -> int:
        """体积上限（与 ``/voice/*`` 共用同一常量）。"""
        return self._max_bytes

    def get(self, ticket_id: str) -> UploadTicket | None:
        """按 id 取票据。"""
        return self._issued.get(ticket_id)

    def issued_refs(self) -> tuple[str, ...]:
        """全部已签发引用（测试/排查用）。"""
        return tuple(item.audio_object_ref for item in self._issued.values())


def reject_audio_payload(payload: Mapping[str, Any]) -> None:
    """最后一道防线：请求体里出现音频字段就直接拒（防止绕过 Pydantic 的路径）。

    Raises:
        ValueError: 出现被禁止的音频字段（映射 ``E_VALIDATION``）。
    """
    forbidden = sorted(key for key in payload if str(key).lower() in FORBIDDEN_AUDIO_KEYS)
    if forbidden:
        raise ValueError(
            "本接口不接收音频字节（callContainer 请求体上限 100KiB）："
            f"请删除字段 {forbidden}，改用 audio_object_ref"
        )
