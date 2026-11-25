"""进货单拍照识别多模态 Vision Provider 抽象（聚焦中国开源视觉模型）。

推荐对接（均兼容 OpenAI Chat Completions multimodal ``image_url`` 协议）：
- 硅基流动 SiliconFlow：``Qwen/Qwen2.5-VL-72B-Instruct`` / ``Qwen/Qwen2.5-VL-7B-Instruct``
- 阿里百炼 Bailian：``qwen2.5-vl-72b-instruct`` / ``qwen-vl-max``（传 ``enable_thinking=False``）
- 智谱开放平台：``glm-4v-flash`` / ``glm-4v-plus``

三条硬约束：
1. ``VISION_PROVIDER=real`` 但缺 ``VISION_API_KEY / VISION_BASE_URL / VISION_MODEL`` 时构造期即报错；
2. 生产环境禁止 ``FakeVisionProvider``；
3. 识别结果**绝不直接入账**，必须产出结构化草稿交由确认页核对并走 ``LedgerService`` 落账。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "ExtractedNoteLine",
    "ExtractedDeliveryNote",
    "FakeVisionProvider",
    "RealVisionProvider",
    "VisionProvider",
    "VisionProviderError",
    "VisionTimeoutError",
    "build_vision_provider",
]

_SYSTEM_PROMPT = (
    "你是中国小微商铺进货单识别助手。请从进货单照片/票据里提取结构化 JSON：\n"
    "{\n"
    '  "supplier_name": "供应商名或null",\n'
    '  "doc_date": "YYYY-MM-DD或null",\n'
    '  "payment_status": "已结或未结",\n'
    '  "stated_total_fen": 整数分或null,\n'
    '  "lines": [\n'
    '    {"product_name": "商品名", "unit": "箱/件/瓶/斤/袋等", "quantity": 数字, '
    '"unit_price_fen": 整数分或null, "amount_fen": 整数分或null}\n'
    "  ],\n"
    '  "notes": "手写备注或空串"\n'
    "}\n"
    "要求：所有金额统一换算成【整数分】（例如 35.5 元 = 3550，绝不用小数）；只输出纯 JSON。"
)


class VisionProviderError(RuntimeError):
    """视觉模型调用失败。"""

    error_code = "E_VISION_FAILED"


class VisionTimeoutError(VisionProviderError):
    """视觉模型超时。"""

    error_code = "E_LLM_TIMEOUT"


@dataclass(frozen=True)
class ExtractedNoteLine:
    """识别出的单行明细。"""

    product_name: str
    unit: str
    quantity: float
    unit_price_fen: int | None = None
    amount_fen: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_name": self.product_name,
            "unit": self.unit,
            "quantity": self.quantity,
            "unit_price_fen": self.unit_price_fen,
            "amount_fen": self.amount_fen,
        }


@dataclass(frozen=True)
class ExtractedDeliveryNote:
    """进货单图片识别结果（草稿）。"""

    supplier_name: str | None = None
    doc_date: str | None = None
    payment_status: str = "已结"
    stated_total_fen: int | None = None
    lines: tuple[ExtractedNoteLine, ...] = field(default_factory=tuple)
    notes: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "supplier_name": self.supplier_name,
            "doc_date": self.doc_date,
            "payment_status": self.payment_status,
            "stated_total_fen": self.stated_total_fen,
            "lines": [line.as_dict() for line in self.lines],
            "notes": self.notes,
            "model": self.model,
        }


@runtime_checkable
class VisionProvider(Protocol):
    """视觉识别协议。"""

    def extract(
        self,
        *,
        image_url: str | None = None,
        image_base64: str | None = None,
        object_ref: str | None = None,
        hint_text: str | None = None,
    ) -> ExtractedDeliveryNote:
        ...


def _to_fen(value: Any, *, yuan_fallback: Any = None) -> int | None:
    if value is not None and not isinstance(value, bool):
        try:
            num = float(value)
            if num >= 0:
                return int(round(num))
        except (TypeError, ValueError):
            pass
    if yuan_fallback is not None and not isinstance(yuan_fallback, bool):
        try:
            num = float(yuan_fallback)
            if num >= 0:
                return int(round(num * 100))
        except (TypeError, ValueError):
            pass
    return None


def _strip_json_fences(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    return text


def _normalize_extracted(payload: Mapping[str, Any], *, model: str) -> ExtractedDeliveryNote:
    raw_lines = payload.get("lines") or []
    lines: list[ExtractedNoteLine] = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            if not isinstance(item, Mapping):
                continue
            pname = str(item.get("product_name") or item.get("name") or "").strip()
            if not pname:
                continue
            unit = str(item.get("unit") or "件").strip() or "件"
            try:
                qty = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            unit_price_fen = _to_fen(
                item.get("unit_price_fen"),
                yuan_fallback=item.get("unit_price_yuan"),
            )
            amount_fen = _to_fen(
                item.get("amount_fen"),
                yuan_fallback=item.get("amount_yuan"),
            )
            if amount_fen is None and unit_price_fen is not None:
                amount_fen = int(round(qty * unit_price_fen))
            elif unit_price_fen is None and amount_fen is not None and qty > 0:
                unit_price_fen = int(round(amount_fen / qty))
            lines.append(
                ExtractedNoteLine(
                    product_name=pname,
                    unit=unit,
                    quantity=int(qty) if float(qty).is_integer() else qty,
                    unit_price_fen=unit_price_fen,
                    amount_fen=amount_fen,
                )
            )
    ps_raw = str(payload.get("payment_status") or "已结").strip()
    payment_status = "未结" if ps_raw in ("未结", "赊账", "挂账", "欠款") else "已结"
    stated_total_fen = _to_fen(
        payload.get("stated_total_fen"),
        yuan_fallback=payload.get("stated_total_yuan"),
    )
    supplier_raw = payload.get("supplier_name")
    supplier = str(supplier_raw).strip() if supplier_raw not in (None, "", "null") else None
    doc_date_raw = payload.get("doc_date")
    doc_date = str(doc_date_raw).strip() if doc_date_raw not in (None, "", "null") else None
    if doc_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", doc_date):
        doc_date = None
    return ExtractedDeliveryNote(
        supplier_name=supplier,
        doc_date=doc_date,
        payment_status=payment_status,
        stated_total_fen=stated_total_fen,
        lines=tuple(lines),
        notes=str(payload.get("notes") or "").strip(),
        model=model,
        raw=dict(payload),
    )


class FakeVisionProvider:
    """离线/单测专用假视觉模型（生产禁止启动）。"""

    provider_name = "fake"

    def __init__(
        self,
        *,
        fixtures: Mapping[str, Mapping[str, Any]] | None = None,
        default: Mapping[str, Any] | None = None,
        is_production: bool = False,
    ) -> None:
        if is_production:
            raise RuntimeError(
                "FakeVisionProvider 禁止在生产环境使用；请配置 VISION_PROVIDER=real 并注入真实开源视觉模型凭证"
            )
        self._fixtures = dict(fixtures or {})
        self._default = dict(
            default
            or {
                "supplier_name": "兴旺批发部",
                "payment_status": "已结",
                "stated_total_fen": 11500,
                "lines": [
                    {
                        "product_name": "可乐",
                        "unit": "箱",
                        "quantity": 2,
                        "unit_price_fen": 3500,
                        "amount_fen": 7000,
                    },
                    {
                        "product_name": "矿泉水",
                        "unit": "箱",
                        "quantity": 3,
                        "unit_price_fen": 1500,
                        "amount_fen": 4500,
                    },
                ],
                "notes": "测试/开发环境模拟识别结果，请核对数量与单价后再确认入账",
            }
        )

    def extract(
        self,
        *,
        image_url: str | None = None,
        image_base64: str | None = None,
        object_ref: str | None = None,
        hint_text: str | None = None,
    ) -> ExtractedDeliveryNote:
        key_text = " ".join(part for part in (object_ref, image_url, hint_text) if part)
        for key in sorted(self._fixtures, key=len, reverse=True):
            if key and key in key_text:
                return _normalize_extracted(self._fixtures[key], model="fake-vision-v1")
        if hint_text and hint_text.strip().startswith("{"):
            try:
                parsed = json.loads(hint_text)
                if isinstance(parsed, Mapping):
                    return _normalize_extracted(parsed, model="fake-vision-v1")
            except ValueError:
                pass
        del image_base64
        return _normalize_extracted(self._default, model="fake-vision-v1")


class RealVisionProvider:
    """真实中国开源多模态视觉模型（Qwen2.5-VL / GLM-4V / OpenAI 兼容）。"""

    provider_name = "real"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        vendor: str = "openai_compat",
        timeout_seconds: float = 20.0,
        client: Any = None,
    ) -> None:
        missing = [
            name
            for name, val in (
                ("VISION_API_KEY", api_key),
                ("VISION_BASE_URL", base_url),
                ("VISION_MODEL", model),
            )
            if not (val or "").strip()
        ]
        if missing:
            raise ValueError(
                "RealVisionProvider 缺少必要配置: " + ", ".join(missing)
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._vendor = (vendor or "openai_compat").strip().lower()
        self._timeout_seconds = timeout_seconds
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self._timeout_seconds,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    def extract(
        self,
        *,
        image_url: str | None = None,
        image_base64: str | None = None,
        object_ref: str | None = None,
        hint_text: str | None = None,
    ) -> ExtractedDeliveryNote:
        import httpx

        resolved_url = (image_url or "").strip()
        if not resolved_url and image_base64:
            raw_b64 = image_base64.strip()
            resolved_url = (
                raw_b64
                if raw_b64.startswith("data:image/")
                else f"data:image/jpeg;base64,{raw_b64}"
            )
        if not resolved_url and object_ref and object_ref.startswith(("http://", "https://", "data:image/")):
            resolved_url = object_ref
        if not resolved_url:
            raise VisionProviderError("缺少可识别的进货单图片（image_base64 或 image_url）")

        user_prompt = "请识别这张进货单上的商品名称、数量、单位、单价、行金额与整单合计金额。"
        if hint_text:
            user_prompt += f"\n商户补充说明：{hint_text}"

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": resolved_url}},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 1500,
            "temperature": 0,
        }
        if self._vendor == "bailian":
            payload["enable_thinking"] = False

        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is None and getattr(httpx.post, "__module__", "") != "httpx._api":
                response = httpx.post(
                    url, headers=headers, json=payload, timeout=self._timeout_seconds
                )
            else:
                response = self._get_client().post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(_strip_json_fences(content))
        except httpx.TimeoutException as exc:
            raise VisionTimeoutError(f"视觉识别超时（>{self._timeout_seconds}s）") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VisionProviderError(f"视觉识别返回无法解析为 JSON: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise VisionProviderError(f"视觉识别调用失败: {exc}") from exc

        if not isinstance(parsed, Mapping):
            raise VisionProviderError("视觉识别返回值不是 JSON 对象")
        return _normalize_extracted(parsed, model=self._model)


def build_vision_provider(settings: Any) -> VisionProvider:
    """按 Settings 装配进货单视觉模型（支持 fake / real，不 fallback 到假实现）。"""
    kind = str(getattr(settings, "vision_provider", "fake") or "fake").strip().lower()
    is_prod = str(getattr(settings, "env", "dev") or "dev").strip().lower() in ("prod", "production")
    if kind == "real":
        return RealVisionProvider(
            api_key=str(getattr(settings, "vision_api_key", "") or ""),
            base_url=str(getattr(settings, "vision_base_url", "") or ""),
            model=str(getattr(settings, "vision_model", "") or ""),
            vendor=str(getattr(settings, "vision_vendor", "openai_compat") or "openai_compat"),
            timeout_seconds=float(getattr(settings, "vision_timeout_seconds", 20.0) or 20.0),
        )
    return FakeVisionProvider(is_production=is_prod)
