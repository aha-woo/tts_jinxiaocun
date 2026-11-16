"""发货推送验签：**生产必须用微信支付平台证书**（本项目不把 HMAC 当默认值）。

为什么不能拿 HMAC 当生产默认
--------------------------
T03 的 ``HmacSignatureVerifier`` 是**沙箱/测试**用的对称密钥验签：
密钥只要泄漏（代码库、日志、镜像层），任何人都能伪造一条「已付款」推送，
给自己发 800 次额度。真实链路必须用微信支付平台证书做**非对称验签**：
私钥在微信侧，我们只持有公钥证书，泄漏也伪造不了签名。

三种实现
-------
- ``FakePlatformCertVerifier``：测试用，**构造时就必须显式声明是 fake**，
  生产环境构造即抛异常（照 ``FakeLlmProvider`` / ``FakeAsrProvider`` 那套）；
- ``RealPlatformCertVerifier``：缺证书 / 缺序列号 → **构造期直接炸**（fail-fast），
  绝不允许「没配好就当通过」；
- ``RotatingVerifier``：证书轮换钩子——按 ``Wechatpay-Serial`` 选当前有效证书，
  老证书保留一段重叠期（微信会新旧并行推送）。

``fail-closed`` 是底线：验签器无法判定时一律返回 ``False``（拒发货 + 报警），
**不存在「验签失败也放过去」的分支**。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.config import Settings
from app.pay.deliver_callback import DeliverPayload, SignatureVerifier

__all__ = [
    "CertEntry",
    "FakePlatformCertVerifier",
    "PlatformCertStore",
    "RealPlatformCertVerifier",
    "RotatingVerifier",
    "build_verifier",
    "serial_of",
]

#: 平台证书序列号在推送头里的字段名（微信支付口径）
SERIAL_HEADER = "Wechatpay-Serial"


class VerifierConfigError(RuntimeError):
    """验签器配置错误（缺证书 / 生产环境用了 fake）。"""


def serial_of(payload: DeliverPayload) -> str:
    """从推送里取证书序列号（兼容 header 与 body 两种带法）。"""
    raw = dict(payload.raw or {})
    for key in (SERIAL_HEADER, "wechatpay_serial", "serial_no", "SerialNo"):
        value = raw.get(key)
        if value:
            return str(value).strip()
    return ""


@dataclass(frozen=True)
class CertEntry:
    """一张平台证书（序列号 + 公钥材料 + 生效区间）。"""

    serial_no: str
    public_key_pem: str
    #: 生效/失效时间戳（秒）；``None`` 表示不限制
    not_before: float | None = None
    not_after: float | None = None

    def valid_at(self, now: float | None = None) -> bool:
        """该证书在此刻是否可用。"""
        moment = now if now is not None else time.time()
        if self.not_before is not None and moment < self.not_before:
            return False
        if self.not_after is not None and moment > self.not_after:
            return False
        return True


class PlatformCertStore:
    """平台证书仓库（支持轮换：新旧证书并存一段时间）。"""

    def __init__(self, entries: Sequence[CertEntry] = ()) -> None:
        """
        Args:
            entries: 初始证书列表。
        """
        self._entries: dict[str, CertEntry] = {entry.serial_no: entry for entry in entries}

    def add(self, entry: CertEntry) -> None:
        """加一张证书（轮换时用：新证书先加进来，老证书过一阵再删）。"""
        self._entries[entry.serial_no] = entry

    def remove(self, serial_no: str) -> None:
        """删一张证书（轮换完成、重叠期结束后调用）。"""
        self._entries.pop(serial_no, None)

    def get(self, serial_no: str) -> CertEntry | None:
        """按序列号取证书。"""
        return self._entries.get(serial_no)

    def current(self, now: float | None = None) -> CertEntry | None:
        """当前有效的最新一张（缺序列号时的兜底选择）。"""
        candidates = [entry for entry in self._entries.values() if entry.valid_at(now)]
        return candidates[-1] if candidates else None

    def serials(self) -> tuple[str, ...]:
        """全部序列号。"""
        return tuple(self._entries)


class FakePlatformCertVerifier(SignatureVerifier):
    """测试用验签器（**仅本地与测试，生产禁用**）。

    验签规则刻意做成「可预测但不可默认通过」：要求推送里带 ``Sign`` 且等于
    ``sha256(f"{out_trade_no}|{goods_id}|{cert}")``。测试可以用 :meth:`sign` 造签名。
    """

    def __init__(self, *, is_production: bool = False, cert: str = "FAKE-SERIAL-001") -> None:
        """
        Args:
            is_production: 传 ``True`` 直接抛异常（防止被误配到生产）。
            cert: 模拟的证书序列号。

        Raises:
            RuntimeError: 生产环境使用 fake 验签器。
        """
        if is_production:
            raise RuntimeError(
                "FakePlatformCertVerifier 仅限本地与测试：生产必须用微信支付平台证书验签"
            )
        self.cert = cert
        self.is_fake = True

    def sign(self, payload: DeliverPayload) -> str:
        """生成测试签名。"""
        message = f"{payload.out_trade_no}|{payload.goods_id}|{self.cert}"
        return hashlib.sha256(message.encode("utf-8")).hexdigest()

    def verify(self, payload: DeliverPayload) -> bool:
        """常量时间比较签名（缺签名一律失败）。"""
        if not payload.signature:
            return False
        return hmac.compare_digest(self.sign(payload), payload.signature.strip())

    @property
    def is_sandbox_only(self) -> bool:
        """明确标记：**不可用于生产**。"""
        return True


class RealPlatformCertVerifier(SignatureVerifier):
    """生产验签器：微信支付平台证书（RSA-SHA256）。

    本机没有真实证书，所以这里**不做联网取证书**，而是要求由部署侧注入：
    ``WX_PLATFORM_CERT_PATH``（PEM 文件）与 ``WX_PLATFORM_CERT_SERIAL``。

    构造期校验（fail-fast）：
    - 缺路径 / 缺序列号 → :class:`VerifierConfigError`；
    - 文件不存在 → :class:`VerifierConfigError`；
    - 证书内容为空 → :class:`VerifierConfigError`。

    验签时若 ``cryptography`` 不可用或证书解析失败，**返回 False**（fail-closed），
    绝不「解析失败就当通过」。
    """

    def __init__(
        self,
        *,
        cert_path: str,
        serial_no: str,
        store: PlatformCertStore | None = None,
    ) -> None:
        """
        Args:
            cert_path: 平台证书 PEM 路径。
            serial_no: 对应序列号。
            store: 证书仓库（支持轮换）。

        Raises:
            VerifierConfigError: 配置缺失或证书不可读。
        """
        if not cert_path or not serial_no:
            raise VerifierConfigError(
                "RealPlatformCertVerifier 需要 WX_PLATFORM_CERT_PATH 与 "
                "WX_PLATFORM_CERT_SERIAL（生产不得用 HMAC 兜底）"
            )
        from pathlib import Path

        path = Path(cert_path)
        if not path.is_file():
            raise VerifierConfigError(f"平台证书不存在：{cert_path}")
        pem = path.read_text(encoding="utf-8").strip()
        if "BEGIN CERTIFICATE" not in pem:
            raise VerifierConfigError(f"平台证书格式不对（不是 PEM）：{cert_path}")

        self._cert_path = str(path)
        self._serial_no = serial_no
        self._pem = pem
        self._store = store or PlatformCertStore([CertEntry(serial_no, pem)])

    @property
    def is_fake(self) -> bool:
        """明确标记：真实验签器。"""
        return False

    def verify(self, payload: DeliverPayload) -> bool:
        """验签（``Wechatpay-Serial`` 必须命中已知证书）。"""
        if not payload.signature:
            return False
        serial_no = serial_of(payload) or self._serial_no
        entry = self._store.get(serial_no)
        if entry is None or not entry.valid_at():
            # 未知序列号 / 证书过期：拒（可能是轮换未完成或伪造）
            return False
        message = "|".join(
            (
                payload.out_trade_no,
                payload.goods_id,
                payload.timestamp,
                payload.nonce,
            )
        )
        return _rsa_sha256_verify(entry.public_key_pem, message, payload.signature)

    def rotate(self, entry: CertEntry) -> None:
        """证书轮换：加入新证书（老证书保留重叠期，不立刻删）。"""
        self._store.add(entry)

    def retire(self, serial_no: str) -> None:
        """轮换完成：退役老证书。"""
        self._store.remove(serial_no)


class RotatingVerifier(SignatureVerifier):
    """按推送里的序列号在证书仓库里选证书验签（多证书并存期用）。"""

    def __init__(self, store: PlatformCertStore, *, clock: Callable[[], float] | None = None) -> None:
        """
        Args:
            store: 证书仓库。
            clock: 注入时钟（测试过期逻辑）。
        """
        self._store = store
        self._clock = clock or time.time

    def verify(self, payload: DeliverPayload) -> bool:
        """验签。"""
        if not payload.signature:
            return False
        serial_no = serial_of(payload)
        entry = self._store.get(serial_no) if serial_no else self._store.current(self._clock())
        if entry is None or not entry.valid_at(self._clock()):
            return False
        message = "|".join(
            (payload.out_trade_no, payload.goods_id, payload.timestamp, payload.nonce)
        )
        return _rsa_sha256_verify(entry.public_key_pem, message, payload.signature)


def _rsa_sha256_verify(public_key_pem: str, message: str, signature: str) -> bool:
    """RSA-SHA256 验签（缺库 / 解析失败一律 False，fail-closed）。"""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError:  # pragma: no cover - 生产镜像会装 cryptography
        return False

    try:
        if "BEGIN CERTIFICATE" in public_key_pem:
            certificate = load_pem_x509_certificate(public_key_pem.encode("utf-8"))
            public_key = certificate.public_key()
        else:
            public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        public_key.verify(  # type: ignore[call-arg]
            bytes.fromhex(signature),
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def build_verifier(settings: Settings) -> SignatureVerifier:
    """按配置构造验签器：**生产缺证书直接抛异常，绝不回落到 HMAC**。

    Args:
        settings: 运行期配置。

    Returns:
        :class:`~app.pay.deliver_callback.SignatureVerifier`。

    Raises:
        VerifierConfigError: 生产环境缺平台证书。
    """
    kind = (getattr(settings, "pay_verifier", "") or "").strip().lower()
    cert_path = getattr(settings, "wx_platform_cert_path", "") or ""
    serial_no = getattr(settings, "wx_platform_cert_serial", "") or ""

    if kind == "platform_cert":
        return RealPlatformCertVerifier(cert_path=cert_path, serial_no=serial_no)
    if kind == "fake":
        if settings.is_production():
            raise VerifierConfigError(
                "生产环境不允许 PAY_VERIFIER=fake：必须配置微信支付平台证书"
            )
        return FakePlatformCertVerifier(is_production=False)
    # 未配置：生产直接炸；本地默认 fake（显式、可发现）
    if settings.is_production():
        raise VerifierConfigError(
            "生产环境必须显式配置 PAY_VERIFIER=platform_cert 并提供平台证书；"
            "不存在「默认 HMAC 也能跑」的路径"
        )
    return FakePlatformCertVerifier(is_production=False)
