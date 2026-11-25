"""应用配置与共享契约装载（单一真相源入口）。

设计要点
--------
1. 契约文件只存在于 ``packages/shared/``，服务端**只读、不复制**，避免双份漂移；
2. 启动即校验契约数量（38 intent / 18 slot / 5 source / 错误码 >= 20），
   不符直接 ``ContractError`` -> fail-fast（对应架构 §8.3 T01 验收 ③）；
3. 所有阈值/成本类常量必须带「假设，需核实」注释，禁止写死厂商价格。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

# 期望的契约规模（契约变更必须先改文档再改这里，测试会二次校验）
EXPECTED_INTENTS = 38
EXPECTED_SLOTS = 18
EXPECTED_SOURCES = 5
EXPECTED_DOMAINS = 12
MIN_ERROR_CODES = 20

#: 数据层表数（含 M0 新增 cost_adjustment / gap / attachment 共 42 张）
EXPECTED_TABLES = 42

#: PG 上的 RLS 策略总数（单一来源，迁移与 ``/readyz`` 都读它）
#: 组成：40 张租户表 × 1 条 ``*_tenant_isolation`` + 2 条引导策略 = 42。
EXPECTED_RLS_POLICIES = 42

#: 单一真相源所在的仓库相对路径（server/app/config.py -> 仓库根）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED_ROOT = _REPO_ROOT / "packages" / "shared"


class ContractError(RuntimeError):
    """契约文件缺失/数量不符/字段非法时抛出（fail-fast，不允许带病启动）。"""


@dataclass(frozen=True)
class ErrorSpec:
    """一条错误码定义。"""

    code: str
    numeric: int
    http_status: int
    category: str
    message_zh: str
    deduct_quota: bool
    client_action: str
    comment: str | None = None


@dataclass(frozen=True)
class Settings:
    """运行期配置（云托管通过环境变量注入）。"""

    app_name: str = "voice-inventory-lite"
    #: 应用版本串。**用环境变量 ``APP_VERSION`` 注入**，方便在系统自检里看出跑的是哪次构建
    #: （默认值 ``0.1.0-T01`` 是历史常量，容易误以为是「部署版本号」——实际云托管的
    #: 部署版本是 001/002/…，两者无关。部署时设 ``APP_VERSION=011`` 即可对上）。
    version: str = os.getenv("APP_VERSION", "0.1.0-T01")
    env: str = os.getenv("APP_ENV", "local")
    contracts_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("CONTRACTS_DIR", str(_SHARED_ROOT))
        )
    )
    # 幂等键 TTL：与「60s 去重」口径一致（架构 §2.3 / §8.4）
    idempotency_ttl_seconds: int = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "60"))
    idempotency_max_entries: int = int(os.getenv("IDEMPOTENCY_MAX_ENTRIES", "2048"))
    # Redis 可用则用 SETNX，否则自动降级内存 LRU（云托管不部署有状态服务，见 D7/§4.5）
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_key_prefix: str = os.getenv("REDIS_KEY_PREFIX", "vil:idem")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # callContainer 请求体上限 100K 断言（D11）
    max_request_body_bytes: int = 100 * 1024

    # ---- 数据层（T02）----
    # 生产：外挂托管 PostgreSQL（方案 A，RLS）；本地默认 SQLite 内存库（无 RLS，如实标注）
    database_url: str = os.getenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    database_echo: bool = os.getenv("DATABASE_ECHO", "0") == "1"

    # ---- 鉴权（T02）----
    # JWT 密钥必须由环境注入；默认值仅用于本地/测试，生产缺失时应视为部署错误
    jwt_secret: str = os.getenv("JWT_SECRET", "local-dev-only-change-me")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expire_seconds: int = int(os.getenv("JWT_EXPIRE_SECONDS", "7200"))

    # ---- 微信登录 provider（T02）----
    # fake = 本地/测试（禁止生产）；real = 调 jscode2session
    wx_provider: str = os.getenv("WX_PROVIDER", "fake")
    wx_appid: str = os.getenv("WX_APPID", "")
    wx_app_secret: str = os.getenv("WX_APP_SECRET", "")
    #: 服务间令牌（云函数 → 云托管内网调用发货推送）。**未配置即拒**：
    #: 发货回调是「付了钱发额度」的入口，不能靠「没配就放行」。
    service_token: str = os.getenv("SERVICE_TOKEN", "")
    wx_api_base_url: str = os.getenv("WX_API_BASE_URL", "https://api.weixin.qq.com")
    wx_timeout_seconds: float = float(os.getenv("WX_TIMEOUT_SECONDS", "5"))

    # ---- LLM 兜底（T03，G4 唯一计费点）----
    # 默认 fake：**永不联网**，避免本地/CI 误调现网烧钱；real 必须显式注入凭证
    llm_provider: str = os.getenv("LLM_PROVIDER", "fake")
    llm_vendor: str = os.getenv("LLM_VENDOR", "openai_compat")
    llm_model: str = os.getenv("LLM_MODEL", "fake-parse-v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_backup_api_key: str = os.getenv("LLM_BACKUP_API_KEY", "")
    llm_backup_base_url: str = os.getenv("LLM_BACKUP_BASE_URL", "")
    llm_backup_model: str = os.getenv("LLM_BACKUP_MODEL", "")
    llm_strong_model: str = os.getenv("LLM_STRONG_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "6"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "1024"))
    vision_provider: str = os.getenv("VISION_PROVIDER", "fake")
    vision_vendor: str = os.getenv("VISION_VENDOR", "openai_compat")
    vision_model: str = os.getenv("VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct")
    vision_api_key: str = os.getenv("VISION_API_KEY", "")
    vision_base_url: str = os.getenv("VISION_BASE_URL", "")
    vision_timeout_seconds: float = float(os.getenv("VISION_TIMEOUT_SECONDS", "20"))
    #: Schema 校验失败后的修正重试次数（PRD 4.4：最多 1 次；失败不扣次）
    llm_schema_retry: int = int(os.getenv("LLM_SCHEMA_RETRY", "1"))
    #: prompt 前缀缓存（Q15）：静态前缀 + 契约版本做 key，命中即省一次 in 计费
    prompt_cache_max_entries: int = int(os.getenv("PROMPT_CACHE_MAX_ENTRIES", "256"))
    prompt_cache_ttl_seconds: int = int(os.getenv("PROMPT_CACHE_TTL_SECONDS", "3600"))
    #: 注入预算（架构 §4 ④）：个人/门店高频商品 Top N，禁止全量档案
    prompt_top_products: int = int(os.getenv("PROMPT_TOP_PRODUCTS", "20"))
    prompt_recent_contexts: int = int(os.getenv("PROMPT_RECENT_CONTEXTS", "3"))

    # ---- ASR（T03）----
    # 同样默认 fake；签名只吃 audio_object_ref，**不接受任何音频字节**
    asr_provider: str = os.getenv("ASR_PROVIDER", "fake")
    asr_endpoint: str = os.getenv("ASR_ENDPOINT", "")
    asr_api_key: str = os.getenv("ASR_API_KEY", "")
    asr_timeout_seconds: float = float(os.getenv("ASR_TIMEOUT_SECONDS", "8"))

    # ---- 成本护栏（T03）----
    # 租户级令牌桶：30/min + 1000/day（PRD 假设值，需核实）
    quota_per_minute: int = int(os.getenv("QUOTA_PER_MINUTE", "30"))
    quota_per_day: int = int(os.getenv("QUOTA_PER_DAY", "1000"))
    #: 音频上限 100KiB：与 callContainer 文本体上限**同一个常量口径**（D11）
    audio_max_bytes: int = 100 * 1024
    #: 上传票据 TTL（秒）：临时对象短 TTL + 限期删除（需核实合规）
    upload_ticket_ttl_seconds: int = int(os.getenv("UPLOAD_TICKET_TTL_SECONDS", "600"))

    # ---- 卡片 / 撤销（T04）----
    #: 卡片 TTL：30s 超时不落账进待确认（PRD 3.5【假设】）
    card_ttl_seconds: int = int(os.getenv("CARD_TTL_SECONDS", "30"))
    #: 撤销窗口（分钟）【假设】
    reversal_window_minutes: int = int(os.getenv("REVERSAL_WINDOW_MINUTES", "10"))
    #: 导出文件签名 URL TTL（秒）【假设】
    export_signed_url_ttl_seconds: int = int(os.getenv("EXPORT_SIGNED_URL_TTL_SECONDS", "300"))

    # ---- 虚拟支付验签（T04）----
    #: 验签器实现：``platform_cert``（生产）/ ``fake``（本地与测试）。
    #: **生产必须配平台证书**；不存在「默认 HMAC 也能跑」的路径（见 app/pay/sign.py）。
    pay_verifier: str = os.getenv("PAY_VERIFIER", "")
    #: 微信支付平台证书 PEM 路径（生产必填）
    wx_platform_cert_path: str = os.getenv("WX_PLATFORM_CERT_PATH", "")
    #: 平台证书序列号（生产必填）
    wx_platform_cert_serial: str = os.getenv("WX_PLATFORM_CERT_SERIAL", "")
    #: 支付环境：0 现网 / 1 沙箱（沙箱 AppKey 不得用于生产）
    virtual_pay_env: int = int(os.getenv("VIRTUAL_PAY_ENV", "0"))

    def jwt_secret_is_default(self) -> bool:
        """是否仍在用本地默认密钥（`/readyz` 会提示，生产必须替换）。"""
        return self.jwt_secret == "local-dev-only-change-me"

    def llm_is_fake(self) -> bool:
        """LLM 是否走本地假实现（生产环境必须为 False）。"""
        return (self.llm_provider or "fake").strip().lower() == "fake"

    def asr_is_fake(self) -> bool:
        """ASR 是否走本地假实现（生产环境必须为 False）。"""
        return (self.asr_provider or "fake").strip().lower() == "fake"

    def is_production(self) -> bool:
        """是否生产环境（假 provider 在此必须立刻炸，见 llm_provider / asr_provider）。"""
        return (self.env or "").strip().lower() in {"prod", "production"}

    def contracts_path(self) -> Path:
        """契约根目录（必须存在，否则视为部署错误）。"""
        return self.contracts_dir


@dataclass(frozen=True)
class ContractBundle:
    """已装载并校验过的共享契约快照。"""

    intents: tuple[Mapping[str, Any], ...]
    intents_by_id: Mapping[str, Mapping[str, Any]]
    slots: tuple[Mapping[str, Any], ...]
    slot_codes: tuple[str, ...]
    sources: tuple[str, ...]
    domains: tuple[Mapping[str, Any], ...]
    domain_codes: tuple[str, ...]
    error_codes: Mapping[str, ErrorSpec]
    goods: Mapping[str, Any]
    raw: Mapping[str, Any]

    def counts(self) -> dict[str, int]:
        """健康检查对外暴露的契约计数。"""
        return {
            "intents": len(self.intents),
            "slots": len(self.slots),
            "sources": len(self.sources),
            "error_codes": len(self.error_codes),
            "domains": len(self.domains),
        }

    def error_spec(self, code: str) -> ErrorSpec:
        """按字符串错误码取定义；未登记直接报契约错误（防止静默新错误码）。"""
        spec = self.error_codes.get(code)
        if spec is None:
            raise ContractError(f"未登记的错误码 {code}，请先补 packages/shared/contracts/error-codes.json")
        return spec


def _read_json(path: Path) -> Any:
    """读取 JSON 契约文件，失败时给出可定位的错误信息。"""
    if not path.is_file():
        raise ContractError(f"契约文件缺失: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:  # pragma: no cover - 仅在契约被改坏时触发
        raise ContractError(f"契约文件不是合法 JSON: {path} ({exc})") from exc


def load_contracts(contracts_dir: Path | None = None) -> ContractBundle:
    """装载并强校验共享契约。

    契约目录的优先级：显式参数 → 环境变量 ``CONTRACTS_DIR`` → 仓库内默认路径。

    为什么必须读环境变量（2026-11-19 部署踩到）：容器里 ``app/`` 被拷到 ``/app/app``，
    按 ``__file__`` 反推出来的仓库根是 ``/``，于是默认路径变成 ``/packages/shared``，
    而镜像实际把它放在 ``/app/packages/shared``（Dockerfile 已设 ``CONTRACTS_DIR``）。
    迁移（``0002_seed``）调用本函数时不传参，若不读环境变量就会在容器里报
    ``契约文件缺失: /packages/shared/contracts/intents.json``。

    Raises
    ------
    ContractError
        文件缺失、数量不符或字段非法 —— 一律 fail-fast。
    """
    env_dir = os.getenv("CONTRACTS_DIR", "").strip()
    root = contracts_dir if contracts_dir is not None else (Path(env_dir) if env_dir else _SHARED_ROOT)
    contracts_root = root / "contracts"

    intents_doc = _read_json(contracts_root / "intents.json")
    slots_doc = _read_json(contracts_root / "slots.json")
    sources_doc = _read_json(contracts_root / "source-enum.json")
    errors_doc = _read_json(contracts_root / "error-codes.json")
    domains_doc = _read_json(contracts_root / "domains.json")
    goods_doc = _read_json(contracts_root / "goods.json")

    intents: list[Mapping[str, Any]] = list(intents_doc.get("intents", []))
    slots: list[Mapping[str, Any]] = list(slots_doc.get("slots", []))
    sources: list[Mapping[str, Any]] = list(sources_doc.get("sources", []))
    domains: list[Mapping[str, Any]] = list(domains_doc.get("domains", []))
    error_map: Mapping[str, Mapping[str, Any]] = errors_doc.get("codes", {})

    if len(intents) != EXPECTED_INTENTS:
        raise ContractError(f"intent 数量应为 {EXPECTED_INTENTS}，实际 {len(intents)}")
    if len(slots) != EXPECTED_SLOTS:
        raise ContractError(f"槽位数量应为 {EXPECTED_SLOTS}，实际 {len(slots)}")
    if len(sources) != EXPECTED_SOURCES:
        raise ContractError(f"source 枚举应为 {EXPECTED_SOURCES}，实际 {len(sources)}")
    if len(domains) != EXPECTED_DOMAINS:
        raise ContractError(f"业务域应为 {EXPECTED_DOMAINS}，实际 {len(domains)}")
    if len(error_map) < MIN_ERROR_CODES:
        raise ContractError(f"错误码至少 {MIN_ERROR_CODES} 个，实际 {len(error_map)}")

    slot_codes = tuple(str(slot["code"]) for slot in slots)
    if len(set(slot_codes)) != len(slot_codes):
        raise ContractError("槽位 code 存在重复")
    domain_codes = tuple(str(domain["code"]) for domain in domains)
    if len(set(domain_codes)) != len(domain_codes):
        raise ContractError("业务域 code 存在重复")

    intents_by_id: dict[str, Mapping[str, Any]] = {}
    for intent in intents:
        intent_id = str(intent.get("intent_id", ""))
        if not intent_id:
            raise ContractError("存在缺少 intent_id 的 intent")
        if intent_id in intents_by_id:
            raise ContractError(f"intent_id 重复: {intent_id}")
        intents_by_id[intent_id] = intent

        risk = intent.get("risk_level")
        confirm = intent.get("confirm_policy")
        if risk not in {"R0", "R1", "R2", "R3"}:
            raise ContractError(f"{intent_id} 的 risk_level 非法: {risk}")
        if confirm not in {"C0", "C1", "C2", "C3"}:
            raise ContractError(f"{intent_id} 的 confirm_policy 非法: {confirm}")
        if risk == "R3" and (confirm != "C3" or not intent.get("whitelist_blocked")):
            raise ContractError(f"{intent_id}: R3 必须 C3 且 whitelist_blocked=true")
        if risk == "R0" and confirm != "C0":
            raise ContractError(f"{intent_id}: R0 必须 C0")
        if intent.get("domain") not in domain_codes:
            raise ContractError(f"{intent_id} 的 domain 未在 domains.json 中登记: {intent.get('domain')}")
        for code in list(intent.get("required_slots", [])) + list(intent.get("optional_slots", [])):
            if code not in slot_codes:
                raise ContractError(f"{intent_id} 引用了未登记槽位: {code}")
        if not intent.get("sample_utterances"):
            raise ContractError(f"{intent_id} 缺少 sample_utterances")

    error_specs: dict[str, ErrorSpec] = {}
    seen_numeric: dict[int, str] = {}
    for code, spec in error_map.items():
        numeric = int(spec["numeric"])
        if numeric in seen_numeric:
            raise ContractError(
                f"错误码 numeric 重复: {code} 与 {seen_numeric[numeric]} 都是 {numeric}"
            )
        seen_numeric[numeric] = code
        error_specs[code] = ErrorSpec(
            code=code,
            numeric=numeric,
            http_status=int(spec["http_status"]),
            category=str(spec["category"]),
            message_zh=str(spec["message_zh"]),
            deduct_quota=bool(spec.get("deduct_quota", False)),
            client_action=str(spec.get("client_action", "")),
            comment=spec.get("comment"),
        )

    return ContractBundle(
        intents=tuple(intents),
        intents_by_id=intents_by_id,
        slots=tuple(slots),
        slot_codes=slot_codes,
        sources=tuple(str(source["code"]) for source in sources),
        domains=tuple(domains),
        domain_codes=domain_codes,
        error_codes=error_specs,
        goods=goods_doc,
        raw={
            "intents": intents_doc,
            "slots": slots_doc,
            "sources": sources_doc,
            "errors": errors_doc,
            "domains": domains_doc,
            "goods": goods_doc,
        },
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例 Settings。"""
    return Settings()


@lru_cache(maxsize=1)
def get_contracts() -> ContractBundle:
    """进程内单例契约（启动时即装载并校验，失败 fail-fast）。"""
    return load_contracts(get_settings().contracts_path())
