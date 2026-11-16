/**
 * 录音门禁（G0/G1，端侧）：**误触与过短录音一律不发请求、不花钱**。
 *
 * 阈值（架构 §4.2【假设，需核实】）
 * -------------------------------
 * - 最短 800ms：短于它基本是误触；
 * - 软上限 15s：到点提示「快说完了」，允许继续；
 * - 硬上限 20s：强制停止（`callContainer` 与 ASR 都有超时约束）；
 * - VAD tail 300ms：尾部静音够长就自动停，用户不用按第二下；
 * - 全程静音（>=90% 帧无能量）直接丢弃。
 *
 * 所有阈值集中在这里，页面只调用 {@link AudioGate}，不在 WXML 里写魔法数字。
 */

import { energyLevel, isSilentRecording, shouldAutoStop, toBars } from '../utils/vad';

/** 门禁阈值（端侧唯一出处）。 */
export const GATE = {
  minDurationMs: 800,
  softMaxMs: 15_000,
  hardMaxMs: 20_000,
  vadTailMs: 300,
} as const;

/** 一次录音的产出。 */
export interface RecordingResult {
  /** 临时文件路径（**只用于直传对象存储，不进请求体**） */
  tempFilePath: string;
  durationMs: number;
  /** 逐帧能量（0~1） */
  levels: number[];
}

/** 门禁判定结果。 */
export type GateVerdict =
  | { ok: true }
  | { ok: false; reason: 'too_short' | 'silent' | 'too_long'; message: string };

/** 判定一次录音是否值得送去解析（**纯函数**，便于单测）。 */
export function judgeRecording(durationMs: number, levels: number[]): GateVerdict {
  if (durationMs < GATE.minDurationMs) {
    return { ok: false, reason: 'too_short', message: '太短了，按住多说两个字' };
  }
  if (durationMs > GATE.hardMaxMs) {
    return { ok: false, reason: 'too_long', message: '一段最多 20 秒，分两段说更快' };
  }
  if (isSilentRecording(levels)) {
    return { ok: false, reason: 'silent', message: '没听到声音，再试一次' };
  }
  return { ok: true };
}

/** 录音状态快照（页面 `setData` 直接用这几个字段）。 */
export interface GateState {
  /** 是否正在录音 */
  recording: boolean;
  /** 已录时长（毫秒；未录音时为 0） */
  durationMs: number;
  /** 能量条（0~1 归一化后的柱高序列） */
  bars: number[];
  /** 是否已经越过软上限（只提示，不强制停） */
  softLimitReached: boolean;
}

/** 状态回调。 */
export type GateListener = (state: GateState) => void;

/**
 * 录音门禁（封装 `RecorderManager`，对外只暴露「开始/停止 + 状态回调」）。
 *
 * 两种触发方式都支持：**按住说**（touchstart/touchend）和**点一下说**（tap 切换）。
 */
export class AudioGate {
  private manager: WechatMiniprogram.RecorderManager;

  private listener: GateListener | null = null;

  private startedAt = 0;

  private timer: number | null = null;

  private levels: number[] = [];

  private lastLoudAt = 0;

  private recording = false;

  private resolver: ((result: RecordingResult | null) => void) | null = null;

  constructor() {
    this.manager = wx.getRecorderManager();
    this.bind();
  }

  /** 绑定状态回调（页面 onLoad 里调一次）。 */
  onState(listener: GateListener): void {
    this.listener = listener;
  }

  /** 是否正在录音。 */
  get isRecording(): boolean {
    return this.recording;
  }

  /** 开始录音（已在录音中则忽略）。 */
  start(): void {
    if (this.recording) {
      return;
    }
    this.levels = [];
    this.startedAt = Date.now();
    this.lastLoudAt = 0;
    this.recording = true;
    this.manager.start({
      duration: GATE.hardMaxMs,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 48000,
      format: 'mp3',
    });
    this.timer = setInterval(() => this.tick(), 100) as unknown as number;
    this.emit();
  }

  /** 停止录音；返回 `null` 表示被门禁丢弃（不发请求）。 */
  stop(): Promise<RecordingResult | null> {
    if (!this.recording) {
      return Promise.resolve(null);
    }
    return new Promise<RecordingResult | null>((resolve) => {
      this.resolver = resolve;
      this.manager.stop();
    });
  }

  private bind(): void {
    this.manager.onFrameRecorded((frame: { frameBuffer: ArrayBuffer }) => {
      const byteLength = frame.frameBuffer.byteLength;
      const atMs = Date.now() - this.startedAt;
      const level = energyLevel({ byteLength, atMs });
      this.levels.push(level);
      if (level >= 0.05) {
        this.lastLoudAt = atMs;
      }
      // VAD：尾部静音够了就自动停，用户不用按第二下
      if (shouldAutoStop(atMs, this.lastLoudAt, GATE.vadTailMs) && atMs >= GATE.minDurationMs) {
        void this.stop();
      }
    });

    this.manager.onStop((res: { tempFilePath: string; duration: number }) => {
      this.recording = false;
      this.clearTimer();
      const durationMs = res.duration > 0 ? res.duration : Date.now() - this.startedAt;
      this.emit();
      const verdict = judgeRecording(durationMs, this.levels);
      const resolve = this.resolver;
      this.resolver = null;
      if (resolve === null) {
        return;
      }
      if (!verdict.ok) {
        wx.showToast({ title: verdict.message, icon: 'none' });
        resolve(null);
        return;
      }
      resolve({ tempFilePath: res.tempFilePath, durationMs, levels: this.levels.slice() });
    });

    this.manager.onError((error: { errMsg: string }) => {
      this.recording = false;
      this.clearTimer();
      this.emit();
      wx.showToast({ title: `录音失败：${error.errMsg}`, icon: 'none' });
      const resolve = this.resolver;
      this.resolver = null;
      if (resolve !== null) {
        resolve(null);
      }
    });
  }

  private tick(): void {
    const durationMs = Date.now() - this.startedAt;
    if (durationMs >= GATE.hardMaxMs) {
      void this.stop();
      return;
    }
    this.emit();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private emit(): void {
    if (this.listener === null) {
      return;
    }
    const durationMs = this.recording ? Date.now() - this.startedAt : 0;
    this.listener({
      recording: this.recording,
      durationMs,
      bars: toBars(this.levels),
      softLimitReached: durationMs >= GATE.softMaxMs,
    });
  }
}

/** 秒 -> 一位小数文本（录音时长展示）。 */
export function durationText(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}
