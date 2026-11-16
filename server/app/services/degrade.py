"""逐级降级：L0 → L4（**降级不停止服务**，且每一次降级都可查可导出）。

为什么要有明确的档位
--------------------
成本护栏最容易做成「超阈值 → 直接 500」，那不是护栏是事故。
这里的做法是**逐级丢掉最贵的能力，保住用户最需要的「能记账」**：

=====  ==========================================================  ==================
档位    关掉什么                                                    用户还能做什么
=====  ==========================================================  ==================
L0      —                                                          全功能
L1      few-shot 与上下文注入减半（prompt 更省）                    全功能（略降召回）
L2      **LLM 兜底关闭**：只走规则层 + 澄清 + 待办                  高频句式照常录
L3      语音解析关闭（新语音只提示稍后），保留手动录单              手动记账照常
L4      仅只读：查询 / 报表 / 导出，写入一律转待办                   查账与导出照常
=====  ==========================================================  ==================

判定信号（任一触发即降级，取最严格档）
--------------------------------------
1. 额度使用率（当月 ``used_count / free_quota``）；
2. LLM 失败/超时率（滚动窗口）；
3. 成本超预算率（``cost_monitor`` 的单次成本 × 调用量 vs 预算）。

所有阈值都是**产品假设，需核实**，集中在本文件，禁止散落到业务代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "DEGRADE_LEVELS",
    "LEVEL_FEATURES",
    "DegradeController",
    "DegradeDecision",
    "DegradePolicy",
    "FEATURE_FLAGS",
]

#: 全部档位（顺序即严重程度）
DEGRADE_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4")

#: 可被降级关掉的能力（**功能名固定**，前端按名字隐藏入口）
FEATURE_FLAGS: tuple[str, ...] = (
    "llm_fallback",
    "voice_parse",
    "voice_capture",
    "write_ops",
    "prompt_rich",
)

#: 各档位的能力开关（False = 关闭）
LEVEL_FEATURES: Mapping[str, Mapping[str, bool]] = {
    "L0": {
        "llm_fallback": True,
        "voice_parse": True,
        "voice_capture": True,
        "write_ops": True,
        "prompt_rich": True,
    },
    "L1": {
        "llm_fallback": True,
        "voice_parse": True,
        "voice_capture": True,
        "write_ops": True,
        "prompt_rich": False,
    },
    "L2": {
        "llm_fallback": False,
        "voice_parse": True,
        "voice_capture": True,
        "write_ops": True,
        "prompt_rich": False,
    },
    "L3": {
        "llm_fallback": False,
        "voice_parse": False,
        "voice_capture": True,
        "write_ops": True,
        "prompt_rich": False,
    },
    "L4": {
        "llm_fallback": False,
        "voice_parse": False,
        "voice_capture": False,
        "write_ops": False,
        "prompt_rich": False,
    },
}

#: 用户可见的档位说明（**静态话术，不调 AI**）
LEVEL_MESSAGES: Mapping[str, str] = {
    "L0": "正常使用",
    "L1": "为了让响应更快，我减少了参考示例（功能不变）",
    "L2": "额度紧张：先用固定的高频句式，复杂说法稍后再试",
    "L3": "额度用完：语音解析暂停，可以手动录单；下月自动恢复",
    "L4": "额度用完：只能查询与导出，写操作先记成待办",
}


@dataclass(frozen=True)
class DegradePolicy:
    """降级阈值（集中定义，全部为产品假设，需核实）。"""

    #: 额度使用率达到该比例 → L2
    l2_usage_ratio: float = 0.9
    #: 额度用尽 → L3
    l3_usage_ratio: float = 1.0
    #: 连续失败率（滚动窗口）达到该比例 → L2
    l2_error_rate: float = 0.35
    #: 成本超预算率 → L2
    l2_cost_ratio: float = 1.0
    #: 只读档（写入全转待办）由人工/运营开启，默认不自动触发
    force_l4: bool = False
    #: 是否允许 L1（prompt 精简）自动触发
    allow_l1: bool = True

    def as_dict(self) -> Mapping[str, Any]:
        """拍平（审计/配置展示用）。"""
        return {
            "l2_usage_ratio": self.l2_usage_ratio,
            "l3_usage_ratio": self.l3_usage_ratio,
            "l2_error_rate": self.l2_error_rate,
            "l2_cost_ratio": self.l2_cost_ratio,
            "force_l4": self.force_l4,
            "allow_l1": self.allow_l1,
        }


@dataclass(frozen=True)
class DegradeDecision:
    """一次降级判定（可审计）。"""

    level: str
    reasons: tuple[str, ...]
    decided_at: str
    signals: Mapping[str, float] = field(default_factory=dict)

    @property
    def features(self) -> Mapping[str, bool]:
        """当前档位下各能力开关。"""
        return LEVEL_FEATURES[self.level]

    @property
    def message(self) -> str:
        """用户可见说明。"""
        return LEVEL_MESSAGES[self.level]

    def allows(self, feature: str) -> bool:
        """某能力是否可用。"""
        return bool(self.features.get(feature, False))

    def as_dict(self) -> Mapping[str, Any]:
        """拍平（历史查询/导出）。"""
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "decided_at": self.decided_at,
            "signals": dict(self.signals),
            "features": dict(self.features),
            "message": self.message,
        }


class DegradeController:
    """按信号决定档位，并保留**完整历史**（可查可导出）。"""

    def __init__(
        self,
        *,
        policy: DegradePolicy | None = None,
        level: str = "L0",
        max_history: int = 200,
    ) -> None:
        self._policy = policy or DegradePolicy()
        self._level = level if level in DEGRADE_LEVELS else "L0"
        self._history: list[DegradeDecision] = []
        self._max_history = max(1, max_history)
        self._remember(
            DegradeDecision(
                level=self._level,
                reasons=("initial",),
                decided_at=_now_iso(),
                signals={},
            )
        )

    # ------------------------------------------------------------------ 判定
    def decide(
        self,
        *,
        usage_ratio: float = 0.0,
        error_rate: float = 0.0,
        cost_ratio: float = 0.0,
    ) -> DegradeDecision:
        """按三个信号重新判定档位（**只会更严格**，不会自动放宽）。"""
        policy = self._policy
        target = "L0"
        reasons: list[str] = []

        if policy.force_l4:
            target, reasons = "L4", ["force_l4"]
        elif usage_ratio >= policy.l3_usage_ratio:
            target = "L3"
            reasons.append(f"额度用尽（usage_ratio={usage_ratio:.2f}）")
        elif usage_ratio >= policy.l2_usage_ratio:
            target = "L2"
            reasons.append(f"额度接近上限（usage_ratio={usage_ratio:.2f}）")
        if target != "L4":
            if error_rate >= policy.l2_error_rate:
                target = _worse(target, "L2")
                reasons.append(f"LLM 失败率偏高（error_rate={error_rate:.2f}）")
            if cost_ratio >= policy.l2_cost_ratio:
                target = _worse(target, "L2")
                reasons.append(f"成本超预算（cost_ratio={cost_ratio:.2f}）")
            if policy.allow_l1 and error_rate > 0:
                target = _worse(target, "L1")

        # 只升不降：恢复需要人工/运营显式 reset（避免抖动导致体验忽好忽坏）
        new_level = _worse(self._level, target)
        if new_level != self._level or reasons:
            self._level = new_level
            decision = DegradeDecision(
                level=new_level,
                reasons=tuple(reasons) or ("no_change",),
                decided_at=_now_iso(),
                signals={
                    "usage_ratio": round(usage_ratio, 4),
                    "error_rate": round(error_rate, 4),
                    "cost_ratio": round(cost_ratio, 4),
                },
            )
            self._remember(decision)
            return decision
        return self._history[-1]

    # ------------------------------------------------------------------ 查询
    @property
    def level(self) -> str:
        """当前档位。"""
        return self._level

    @property
    def features(self) -> Mapping[str, bool]:
        """当前能力开关。"""
        return LEVEL_FEATURES[self._level]

    @property
    def message(self) -> str:
        """当前档位的用户可见说明。"""
        return LEVEL_MESSAGES[self._level]

    def allows(self, feature: str) -> bool:
        """某能力是否可用（业务代码只准问这一句）。"""
        return bool(self.features.get(feature, False))

    def history(self, *, limit: int = 50) -> tuple[Mapping[str, Any], ...]:
        """降级历史（**可查**）。"""
        return tuple(item.as_dict() for item in self._history[-limit:])

    def export(self) -> tuple[Mapping[str, Any], ...]:
        """导出全部历史（**可导出**，供对账/复盘）。"""
        return tuple(item.as_dict() for item in self._history)

    def reset(self, *, level: str = "L0", reason: str = "manual_reset") -> DegradeDecision:
        """人工恢复（运营补偿；会写一条历史）。"""
        self._level = level if level in DEGRADE_LEVELS else "L0"
        decision = DegradeDecision(
            level=self._level,
            reasons=(reason,),
            decided_at=_now_iso(),
            signals={},
        )
        self._remember(decision)
        return decision

    # ------------------------------------------------------------------ 内部
    def _remember(self, decision: DegradeDecision) -> None:
        """记一条历史（有界）。"""
        self._history.append(decision)
        if len(self._history) > self._max_history:
            del self._history[: len(self._history) - self._max_history]

    def apply_to_imports(self) -> Iterable[str]:
        """当前被关闭的能力名（供前端隐藏入口）。"""
        return tuple(name for name, enabled in self.features.items() if not enabled)


def _worse(current: str, candidate: str) -> str:
    """取更严重的档位。"""
    return current if DEGRADE_LEVELS.index(current) >= DEGRADE_LEVELS.index(candidate) else candidate


def _now_iso() -> str:
    """当前时间（UTC ISO）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
