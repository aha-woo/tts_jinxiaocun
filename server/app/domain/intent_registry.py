"""意图注册表：**只从** ``packages/shared/contracts/intents.json`` 派生（H5 单一真相源）。

为什么要有它
------------
规则层（G3 模板）与 LLM 层（G4 兜底）必须产出**同一份**行为语义：
``risk_level`` / ``confirm_policy`` / ``clarifier_templates`` / ``side_effect`` / ``reversal``
一律从这里读，模板里**不许**重复定义——否则规则层与 LLM 层会慢慢分叉，
出现「规则层不用确认、LLM 层要确认」这种最难查的 bug（架构 §4 ③ H5）。

本模块**不复制**任何契约数据：所有字段都来自进程内已强校验的
:class:`~app.config.ContractBundle`。契约变了，这里自动跟着变。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from app.config import ContractBundle, get_contracts

#: 静态系统意图（本地内容直接返回，不进 G3/G4，见架构 §4 ② 判定链第 3 步）
STATIC_INTENT_IDS: frozenset[str] = frozenset({"sys.help", "sys.example"})

#: R3 白名单动作：语音只能生成待办，任何路径不得直写业务表（PRD 2.7）
R3_INTENTS_FROZEN: frozenset[str] = frozenset()


@dataclass(frozen=True)
class IntentSpec:
    """一个 intent 的完整行为规格（全部来自契约，不含本地补充常量）。"""

    intent_id: str
    name_zh: str
    domain: str
    risk_level: str
    confirm_policy: str
    required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]
    template_hit: bool
    whitelist_blocked: bool
    dynamic_escalation: str | None
    sample_utterances: tuple[str, ...]
    clarifier_templates: Mapping[str, str]
    side_effect: Mapping[str, Any]
    reversal: Mapping[str, Any]
    milestone: str
    seq: int

    @property
    def is_readonly(self) -> bool:
        """R0：只读，不占卡片、不写库（架构 §4 ⑥）。"""
        return self.risk_level == "R0"

    @property
    def is_static(self) -> bool:
        """静态意图：本地话术直接返回，不调 LLM。"""
        return self.intent_id in STATIC_INTENT_IDS

    @property
    def writes_db(self) -> bool:
        """是否会产生写操作（决定是否必须走确认卡片）。"""
        return bool((self.side_effect or {}).get("writes_db", False))

    @property
    def all_slots(self) -> tuple[str, ...]:
        """必填 + 可选（去重保序）。"""
        seen: dict[str, None] = {}
        for code in (*self.required_slots, *self.optional_slots):
            seen.setdefault(code, None)
        return tuple(seen)

    def clarifier_for(self, slot_code: str) -> str | None:
        """该 intent 下某槽位的追问话术（**静态内容，不调 AI**）。"""
        return self.clarifier_templates.get(slot_code)


class IntentRegistry:
    """38 个 intent 的只读注册表。"""

    def __init__(self, bundle: ContractBundle) -> None:
        self._bundle = bundle
        specs: dict[str, IntentSpec] = {}
        for raw in bundle.intents:
            spec = IntentSpec(
                intent_id=str(raw["intent_id"]),
                name_zh=str(raw.get("name_zh", raw["intent_id"])),
                domain=str(raw["domain"]),
                risk_level=str(raw["risk_level"]),
                confirm_policy=str(raw["confirm_policy"]),
                required_slots=tuple(str(code) for code in raw.get("required_slots", ())),
                optional_slots=tuple(str(code) for code in raw.get("optional_slots", ())),
                template_hit=bool(raw.get("template_hit", False)),
                whitelist_blocked=bool(raw.get("whitelist_blocked", False)),
                dynamic_escalation=raw.get("dynamic_escalation"),
                sample_utterances=tuple(str(text) for text in raw.get("sample_utterances", ())),
                clarifier_templates=dict(raw.get("clarifier_templates") or {}),
                side_effect=dict(raw.get("side_effect") or {}),
                reversal=dict(raw.get("reversal") or {}),
                milestone=str(raw.get("milestone", "MVP")),
                seq=int(raw.get("seq", 0)),
            )
            specs[spec.intent_id] = spec
        self._specs = specs
        self._ordered = tuple(sorted(specs.values(), key=lambda item: item.seq))
        self._ids = frozenset(specs)
        self._version = str(bundle.raw.get("intents", {}).get("version", "0"))

    # ------------------------------------------------------------------ 查询
    @property
    def version(self) -> str:
        """契约版本号（参与 prompt 缓存 key，见 Q15）。"""
        return self._version

    @property
    def ids(self) -> frozenset[str]:
        """全部 intent_id。"""
        return self._ids

    def all(self) -> tuple[IntentSpec, ...]:
        """按契约 ``seq`` 排序的全部 intent。"""
        return self._ordered

    def get(self, intent_id: str) -> IntentSpec | None:
        """按 id 取；不存在返回 None（调用方决定报错方式）。"""
        return self._specs.get(intent_id)

    def require(self, intent_id: str) -> IntentSpec:
        """按 id 取；不存在抛 ``KeyError``（契约漂移要立刻炸，不能静默兜住）。"""
        spec = self._specs.get(intent_id)
        if spec is None:
            raise KeyError(f"未登记的 intent_id: {intent_id}")
        return spec

    def exists(self, intent_id: str) -> bool:
        """是否存在。"""
        return intent_id in self._ids

    def template_hit_intents(self) -> tuple[IntentSpec, ...]:
        """18 个 ``template_hit=true`` 的高频意图（必须 100% 不出 LLM）。"""
        return tuple(spec for spec in self._ordered if spec.template_hit)

    def template_hit_ids(self) -> frozenset[str]:
        """``template_hit=true`` 的 id 集合。"""
        return frozenset(spec.intent_id for spec in self._ordered if spec.template_hit)

    def by_domain(self, domain: str) -> tuple[IntentSpec, ...]:
        """按业务域过滤（few-shot 动态取样用）。"""
        return tuple(spec for spec in self._ordered if spec.domain == domain)

    def domains(self) -> tuple[str, ...]:
        """全部业务域（去重保序）。"""
        return tuple(dict.fromkeys(spec.domain for spec in self._ordered))

    def few_shot_examples(self, *, domain: str | None = None, limit: int = 12) -> tuple[IntentSpec, ...]:
        """few-shot 取样：优先命中 domain 的 intent，总条数封顶（架构 §4 ④）。

        Args:
            domain: G3 给出的 domain hint；``None`` 时退化为全域高频。
            limit: 条数上限（默认 12，需核实）。

        Returns:
            用于 prompt 样例段的 intent 子集（**不含**全量商品档案）。
        """
        pool = self.by_domain(domain) if domain else self._ordered
        selected: list[IntentSpec] = []
        for spec in pool:
            if spec.is_static:
                continue
            selected.append(spec)
            if len(selected) >= max(1, limit):
                break
        return tuple(selected)


@lru_cache(maxsize=1)
def default_registry() -> IntentRegistry:
    """进程级默认注册表（读已强校验的契约单例）。"""
    return IntentRegistry(get_contracts())


__all__ = [
    "IntentRegistry",
    "IntentSpec",
    "STATIC_INTENT_IDS",
    "R3_INTENTS_FROZEN",
    "default_registry",
]
