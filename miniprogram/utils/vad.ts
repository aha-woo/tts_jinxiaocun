/**
 * 端侧 VAD（语音活动检测）+ 录音能量评估：**纯本地计算，0 成本**。
 *
 * 用途
 * ----
 * 1. 录音过程中给用户「系统在听」的反馈（能量条）；
 * 2. 判断尾部静音是否够长（VAD tail 300ms），让「说完自动停」可用；
 * 3. 识别「全程静音」的误触，直接丢本地，**不发请求、不花钱**。
 *
 * 阈值来源：架构 §4.2 G1【假设，需核实】：0.8s 下限 / 15s 软上限 / 20s 硬上限 /
 * VAD tail 300ms。这里只做**算法**，具体阈值由 `services/audio-gate.ts` 持有。
 */

/** 录音帧（RecorderManager 回调的一帧）。 */
export interface AudioFrame {
  /** 这一帧的字节数（PCM 大小与能量正相关，作为无解码条件下的近似能量） */
  byteLength: number;
  /** 相对开始时刻的毫秒偏移 */
  atMs: number;
}

/** 能量分档（0~1，端上只用于可视化，不参与计费判定）。 */
export function energyLevel(frame: AudioFrame, baseline = 3200): number {
  if (frame.byteLength <= 0) {
    return 0;
  }
  const ratio = frame.byteLength / baseline;
  return Math.max(0, Math.min(1, ratio));
}

/**
 * 判断从 `lastLoudAtMs` 起是否已经静音够久（尾部静音检测）。
 *
 * @param nowMs 当前时刻（相对录音开始）
 * @param lastLoudAtMs 最后一次「有声音」的时刻
 * @param tailMs 需要的静音时长（默认 300ms）
 * @returns 是否可以自动结束录音
 */
export function shouldAutoStop(nowMs: number, lastLoudAtMs: number, tailMs = 300): boolean {
  return nowMs - lastLoudAtMs >= tailMs;
}

/**
 * 整段录音是否「基本没声音」（误触判定）。
 *
 * @param levels 逐帧能量（0~1）
 * @param silentRatio 判定阈值：静音帧占比超过该值即认为误触（默认 0.9）
 * @returns 是否应丢弃
 */
export function isSilentRecording(levels: number[], silentRatio = 0.9): boolean {
  if (levels.length === 0) {
    return true;
  }
  const silent = levels.filter((level) => level < 0.05).length;
  return silent / levels.length >= silentRatio;
}

/**
 * 把能量序列压成固定长度的柱状图（给 WXML 直接渲染，避免在模板里算）。
 *
 * @param levels 逐帧能量
 * @param bars 目标柱数（默认 12）
 * @returns 每根柱的高度百分比（10~100）
 */
export function toBars(levels: number[], bars = 12): number[] {
  if (levels.length === 0) {
    return new Array<number>(bars).fill(10);
  }
  const bucket = Math.max(1, Math.floor(levels.length / bars));
  const result: number[] = [];
  for (let index = 0; index < bars; index += 1) {
    const slice = levels.slice(index * bucket, (index + 1) * bucket);
    const peak = slice.length > 0 ? Math.max(...slice) : 0;
    result.push(Math.round(10 + peak * 90));
  }
  return result;
}
