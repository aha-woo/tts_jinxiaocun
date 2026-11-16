"""G0/G1 端侧门禁判定（0 成本）—— 服务端只做**同一套阈值**的复核与可测试化。

阈值全部来自 PRD 5.2 / 架构 §4.2，且都是**有意为之的硬编码常量**（不是业务可配项）：

| 项 | 值 | 说明 |
|---|---|---|
| 最短时长 | 0.8s | 低于此判误触，不送 ASR |
| 软上限 | 15s | 到点自动收音（端侧倒计时） |
| 硬上限 | 20s | 强制切断，拒绝继续录 |
| VAD 判停 | 700~900ms 连续静音 | 能力需核实（端侧 VAD vs 平台端点检测） |
| 防吞字 | 300ms | 端点前保留，避免尾字被切掉 |
| 重复短音去重 | 1.5s 内 | 误触抖动 |

**本模块无状态**：端侧把测量值传上来，服务端按同一张表判定，保证
「阈值只在契约里写一遍、测试只测一处」。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MIN_DURATION_MS",
    "SOFT_MAX_MS",
    "HARD_MAX_MS",
    "VAD_MIN_MS",
    "VAD_MAX_MS",
    "VAD_TAIL_MS",
    "REPEAT_TAP_WINDOW_MS",
    "GateThresholds",
    "GateDecision",
    "Gate",
]

#: 最短有效时长（<0.8s 判误触）
MIN_DURATION_MS: int = 800
#: 软上限：到点自动收音
SOFT_MAX_MS: int = 15_000
#: 硬上限：强制切断
HARD_MAX_MS: int = 20_000
#: VAD 判停静音区间（需核实）
VAD_MIN_MS: int = 700
VAD_MAX_MS: int = 900
#: 端点前保留（防吞字）
VAD_TAIL_MS: int = 300
#: 1.5s 内重复短音去重
REPEAT_TAP_WINDOW_MS: int = 1500


@dataclass(frozen=True)
class GateThresholds:
    """门禁阈值（默认即契约口径，测试可构造边界值）。"""

    min_duration_ms: int = MIN_DURATION_MS
    soft_max_ms: int = SOFT_MAX_MS
    hard_max_ms: int = HARD_MAX_MS
    vad_min_ms: int = VAD_MIN_MS
    vad_max_ms: int = VAD_MAX_MS
    vad_tail_ms: int = VAD_TAIL_MS
    repeat_tap_window_ms: int = REPEAT_TAP_WINDOW_MS

    def snapshot(self) -> dict[str, int]:
        """对外暴露的阈值快照（给端侧 / 运维核对同一套数字）。"""
        return {
            "min_duration_ms": self.min_duration_ms,
            "soft_max_ms": self.soft_max_ms,
            "hard_max_ms": self.hard_max_ms,
            "vad_min_ms": self.vad_min_ms,
            "vad_max_ms": self.vad_max_ms,
            "vad_tail_ms": self.vad_tail_ms,
            "repeat_tap_window_ms": self.repeat_tap_window_ms,
        }


@dataclass(frozen=True)
class GateDecision:
    """门禁判定结果。

    ``action`` 语义：
    - ``accept``：正常进入 G2；
    - ``auto_stop``：已到软上限，接受但端侧应立即收音（>硬上限则截到软上限）；
    - ``reject``：拒绝，附 ``error_code``。
    """

    passed: bool
    action: str
    reason: str
    duration_ms: int = 0
    error_code: str | None = None

    @property
    def effective_duration_ms(self) -> int:
        """实际送 ASR 的时长（auto_stop 时被截断到软上限）。"""
        return self.duration_ms


class Gate:
    """G0/G1 判定器（无状态，可并发复用）。"""

    def __init__(self, thresholds: GateThresholds | None = None) -> None:
        self._t = thresholds or GateThresholds()

    @property
    def thresholds(self) -> GateThresholds:
        """当前阈值。"""
        return self._t

    def check(
        self,
        duration_ms: int,
        *,
        now_ms: int,
        last_trigger_ms: int | None = None,
        has_energy: bool = True,
        valid_hanzi: int = 1,
        mode: str = "hold",
    ) -> GateDecision:
        """G0 误触 + G1 时长/能量门禁。

        Args:
            duration_ms: 本次录音时长（毫秒）。
            now_ms: 本次触发时间戳（单调时钟毫秒）。
            last_trigger_ms: 上一次触发时间戳（用于 1.5s 抖动去重）。
            has_energy: 能量是否超过门限（False = 纯环境音）。
            valid_hanzi: 粗判的有效汉字数（0 = 无有效语音）。
            mode: ``hold``（按住说）/ ``tap``（点一下说），两者阈值一致。

        Returns:
            :class:`GateDecision`
        """
        if mode not in {"hold", "tap"}:
            raise ValueError(f"未知录音模式: {mode}")

        duration = max(0, int(duration_ms))

        # G0：1.5s 内重复短音（抖动双击）
        if (
            last_trigger_ms is not None
            and duration < self._t.min_duration_ms
            and 0 <= now_ms - last_trigger_ms < self._t.repeat_tap_window_ms
        ):
            return GateDecision(False, "reject", "repeat_tap", duration, "E_GATE_REJECT")

        # G0：纯环境音 / 无有效汉字（送 ASR 前粗判，省一次 ASR）
        if not has_energy:
            return GateDecision(False, "reject", "no_energy", duration, "E_GATE_REJECT")
        if valid_hanzi < 1:
            return GateDecision(False, "reject", "no_voice", duration, "E_GATE_REJECT")

        # G1：太短 -> 误触
        if duration < self._t.min_duration_ms:
            return GateDecision(False, "reject", "too_short", duration, "E_GATE_REJECT")

        # G1：超硬上限 -> 截到软上限后接受
        if duration > self._t.hard_max_ms:
            return GateDecision(True, "auto_stop", "hard_cut", self._t.soft_max_ms)

        # G1：到软上限 -> 自动收音
        if duration >= self._t.soft_max_ms:
            return GateDecision(True, "auto_stop", "soft_limit", self._t.soft_max_ms)

        return GateDecision(True, "accept", "ok", duration)

    def vad_should_stop(self, silence_ms: int) -> bool:
        """VAD：连续静音 ≥700ms 判停（上限 900ms 仅作端侧兜底）。"""
        return int(silence_ms) >= self._t.vad_min_ms

    def vad_tail_ms(self) -> int:
        """端点前保留时长（防吞字）。"""
        return self._t.vad_tail_ms
