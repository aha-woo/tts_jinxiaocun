"""G6 输出契约：``ParseResult`` 的 Pydantic v2 强校验（规则层与 LLM 层**共用**）。

契约来源：``packages/shared/schemas/parse-result.schema.json``（T01 已冻结）。
规则层命中（channel=template）与 LLM 兜底（channel=llm）都必须过**同一个**校验器——
这是「省钱不省确认」的技术保证（架构 §4 ③ 末段）。

校验内容（架构 §4 ④「校验与重试」）
------------------------------------
1. ``intent`` 必须存在于注册表（38 个之一）；
2. ``source`` 的键必须与 ``slots`` 的键**完全一致**，且值 ∈ 5 枚举
   （``asr/default/computed/alias/pending``）；
3. 金额类槽位（``*_fen`` 或 ``AMT``）必须是**正整数分**（禁止 0 与浮点）；
4. ``U`` 必须落在已登记单位词表内（不允许模型自造单位）；
5. ``D`` 必须是合法 ISO 日期（``YYYY-MM-DD``），禁止「昨天」这类原话直存；
6. ``clarify`` 出现时：``options`` 2~3 个、``question`` 非空（一次只问 1 槽）；
7. ``channel == "computed"`` 的值必须能在 ``calc_traces`` 里找到计算式（禁止黑箱数字）。

失败一律抛 :class:`SchemaValidationError`，其 ``issues`` 可直接回灌给 LLM 做
**最多 1 次**修正重试。
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.intent_registry import IntentRegistry, default_registry
from app.utils.date_utils import is_iso_date
from app.utils.unit import CANONICAL_UNITS

#: ``source`` 五枚举（与 ``packages/shared/contracts/source-enum.json`` 一致）
SOURCE_VALUES: tuple[str, ...] = ("asr", "default", "computed", "alias", "pending")

SourceValue = Literal["asr", "default", "computed", "alias", "pending"]

#: 金额槽位（统一按「整数分」校验）
MONEY_SLOTS: frozenset[str] = frozenset({"UP", "TA", "AMT"})

_MONEY_KEY_PATTERN = re.compile(r"_fen$")


class SchemaValidationError(ValueError):
    """``ParseResult`` 校验失败（映射 ``E_LLM_SCHEMA_INVALID``，502）。"""

    error_code = "E_LLM_SCHEMA_INVALID"

    def __init__(self, issues: list[str], *, raw: Any = None) -> None:
        self.issues = issues
        self.raw = raw
        super().__init__("ParseResult 校验失败: " + "；".join(issues) if issues else "ParseResult 校验失败")


class ClarifyPrompt(BaseModel):
    """追问契约：一次只问 1 个槽位、给 2~3 个可点选项（G5）。"""

    model_config = ConfigDict(extra="forbid")

    slot: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=2, max_length=3)


class ParseResult(BaseModel):
    """解析结果（严格对齐 parse-result.schema.json，``additionalProperties=false``）。"""

    model_config = ConfigDict(extra="forbid")

    intent: str
    slots: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, SourceValue] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    clarify: ClarifyPrompt | None = None
    calc_traces: list[str] = Field(default_factory=list)
    channel: Literal["template", "llm"] | None = None
    unknown_spans: list[str] = Field(default_factory=list)
    route: Literal["card", "answer", "pending_task", "clarify", "static"] | None = Field(
        default=None,
        description="服务端路由结论（非 LLM 输出字段，T04 据此决定卡片/只读/待办）",
    )

    @field_validator("slots", mode="after")
    @classmethod
    def _reject_nested_money(cls, value: dict[str, Any]) -> dict[str, Any]:
        """金额不得以 float 传入（浮点误差会直接变成账差）。"""
        for key, slot_value in value.items():
            if (key in MONEY_SLOTS or _MONEY_KEY_PATTERN.search(key)) and isinstance(slot_value, float):
                raise ValueError(f"槽位 {key} 是金额，必须用整数分，不得用 float")
        return value

    def slot_codes(self) -> frozenset[str]:
        """已填槽位代号集合。"""
        return frozenset(self.slots)


def validate_parse_result(
    payload: Mapping[str, Any] | ParseResult,
    *,
    registry: IntentRegistry | None = None,
) -> ParseResult:
    """把 dict（规则层构造 / LLM 返回）校验成 :class:`ParseResult`。

    Args:
        payload: 待校验对象；可以是 Pydantic 实例（直接返回）。
        registry: 意图注册表（默认进程单例）。

    Returns:
        合法的 :class:`ParseResult`。

    Raises:
        SchemaValidationError: 结构或语义不合法，``issues`` 可直接回灌 LLM 重试。
    """
    if isinstance(payload, ParseResult):
        return payload

    try:
        model = ParseResult.model_validate(dict(payload))
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        ]
        raise SchemaValidationError(issues, raw=payload) from exc

    issues = semantic_issues(model, registry=registry)
    if issues:
        raise SchemaValidationError(issues, raw=payload)
    return model


def semantic_issues(model: ParseResult, *, registry: IntentRegistry | None = None) -> list[str]:
    """结构合法之后的业务语义校验，返回问题列表（空表示通过）。"""
    reg = registry or default_registry()
    issues: list[str] = []

    if not reg.exists(model.intent):
        issues.append(f"intent: 未登记的 intent_id {model.intent!r}")

    slot_keys = set(model.slots)
    source_keys = set(model.source)
    if slot_keys != source_keys:
        missing = sorted(slot_keys - source_keys)
        extra = sorted(source_keys - slot_keys)
        if missing:
            issues.append(f"source: 槽位 {missing} 缺少来源标注（source 五枚举必填）")
        if extra:
            issues.append(f"source: 多余的来源标注 {extra}（该槽位没有值）")

    for code, value in model.slots.items():
        if code in MONEY_SLOTS or _MONEY_KEY_PATTERN.search(code):
            if not isinstance(value, int) or isinstance(value, bool):
                issues.append(f"slots.{code}: 金额必须是整数分（收到 {type(value).__name__}）")
            elif value <= 0:
                issues.append(f"slots.{code}: 金额必须 > 0（收到 {value}）")
        if code == "U" and isinstance(value, str) and value not in CANONICAL_UNITS:
            issues.append(f"slots.U: 单位 {value!r} 不在已登记词表内（不得自造单位）")
        if code == "D" and isinstance(value, str) and not is_iso_date(value):
            issues.append(f"slots.D: 日期必须是 YYYY-MM-DD（收到 {value!r}）")
        if code == "PS" and value not in {"已结", "未结", "部分"}:
            issues.append(f"slots.PS: 付款状态非法 {value!r}")
        if code == "RATIO" and isinstance(value, (int, float)) and value <= 0:
            issues.append("slots.RATIO: 换算系数必须 > 0（不得臆造）")

    computed_slots = [code for code, src in model.source.items() if src == "computed"]
    if computed_slots and not model.calc_traces:
        issues.append(f"calc_traces: 槽位 {sorted(computed_slots)} 标为 computed，必须给出计算式")

    if model.clarify is not None:
        if model.clarify.slot not in model.slots and model.clarify.slot not in _known_slot_codes():
            issues.append(f"clarify.slot: 未知槽位 {model.clarify.slot!r}")
        if len(model.clarify.question.strip()) == 0:
            issues.append("clarify.question: 不能为空")

    spec = reg.get(model.intent)
    if spec is not None and spec.whitelist_blocked:
        # R3 只允许生成待办（PRD 2.7）——不在这里报错，但必须显式路由
        if model.route not in (None, "pending_task", "clarify"):
            issues.append(f"intent: {model.intent} 是 R3 白名单动作，只能生成待办，不得直写")
    return issues


def _known_slot_codes() -> frozenset[str]:
    """18 个槽位代号（契约单一真相源）。"""
    from app.config import get_contracts  # 局部导入：避免模块级循环

    return frozenset(get_contracts().slot_codes)


def ensure_not_direct_write(intent_id: str, *, registry: IntentRegistry | None = None) -> None:
    """写路径守卫：R3 白名单动作**任何路径**不得直写业务表（PRD 2.7 / T04 复用）。

    Raises:
        ValueError: 该 intent 只能生成待办。
    """
    reg = registry or default_registry()
    spec = reg.get(intent_id)
    if spec is not None and spec.whitelist_blocked:
        raise ValueError(f"{intent_id} 属 R3 白名单动作，只能生成待办（pending_task），禁止直写")


__all__ = [
    "MONEY_SLOTS",
    "SOURCE_VALUES",
    "ClarifyPrompt",
    "ParseResult",
    "SchemaValidationError",
    "ensure_not_direct_write",
    "semantic_issues",
    "validate_parse_result",
]
