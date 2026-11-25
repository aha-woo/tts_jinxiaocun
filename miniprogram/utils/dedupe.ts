/**
 * 60 秒去重 + 幂等键生成（端侧）。
 *
 * 两条硬约束（架构 §4.2 G2 / §8.4 幂等）
 * ------------------------------------
 * 1. **60 秒内同一句话不重复解析**：用户连点两次大按钮很常见，重发等于白花钱；
 * 2. 写请求必须带稳定 `Idempotency-Key`：**同一次操作重试要复用同一个键**，
 *    否则服务端会当成两笔业务（重复落账）。
 *
 * 实现故意保持「无网络、无时钟依赖外部」：键由「作用域 + 时间窗序号」拼，
 * 窗口内稳定、跨窗口变化。
 */

const DEDUPE_STORAGE_KEY = 'vi.dedupe.v1';

/** 去重窗口（毫秒）。 */
export const DEDUPE_WINDOW_MS = 60_000;

interface DedupeRecord {
  /** 归一化后的文本 */
  text: string;
  /** 该文本首次出现的时间戳 */
  at: number;
}

/** 文本归一化：去空白、全角转半角逗号句号（避免标点差异导致去重失效）。 */
export function normalizeText(text: string): string {
  return text.replace(/\s+/g, '').replace(/[，。！？、]/g, '').trim();
}

function readRecords(): DedupeRecord[] {
  try {
    const raw = wx.getStorageSync(DEDUPE_STORAGE_KEY);
    return Array.isArray(raw) ? (raw as DedupeRecord[]) : [];
  } catch (error) {
    console.warn('[dedupe] 读缓存失败', error);
    return [];
  }
}

function writeRecords(records: DedupeRecord[]): void {
  try {
    wx.setStorageSync(DEDUPE_STORAGE_KEY, records);
  } catch (error) {
    console.warn('[dedupe] 写缓存失败', error);
  }
}

/**
 * 判断这句话是否在去重窗口内重复。
 *
 * @param text 识别出的文本
 * @param nowMs 当前时间戳（注入便于测试）
 * @returns `true` = 重复（调用方应直接提示「刚记过」而不发请求）
 */
export function isDuplicateText(text: string, nowMs = Date.now()): boolean {
  const normalized = normalizeText(text);
  if (normalized === '') {
    return false;
  }
  const records = readRecords().filter((record) => nowMs - record.at < DEDUPE_WINDOW_MS);
  const hit = records.some((record) => record.text === normalized);
  if (!hit) {
    records.push({ text: normalized, at: nowMs });
    writeRecords(records);
  }
  return hit;
}

/**
 * **只查不记**：这句话在窗口内说过吗？
 *
 * 为什么要有「只查」这一档
 * ----------------------
 * ``isDuplicateText`` 是「查 + 记」合一的：如果拿它做**发请求前**的判重，
 * 用户在弹窗里点了「不记了」，这句话也已经被记进窗口了——
 * 于是他 60 秒内再说一遍同样的真话，会**再次**被当成重复拦一次。
 *
 * 所以判重拆成两段：先用 {@link wasSaidRecently} 查，**确定要发了**再用
 * {@link rememberSpokenText} 记。
 */
export function wasSaidRecently(text: string, nowMs = Date.now()): boolean {
  const normalized = normalizeText(text);
  if (normalized === '') {
    return false;
  }
  const records = readRecords().filter((record) => nowMs - record.at < DEDUPE_WINDOW_MS);
  return records.some((record) => record.text === normalized);
}

/**
 * **只记不查**：把这句话写进去重窗口（**发请求之前**才调）。
 *
 * 记了之后，60 秒内再有人说同一句，就会走「是又记了一笔，还是刚才点重了？」的追问，
 * 而不是**静默吞掉**——真实重复的生意不能被悄悄吃掉。
 */
export function rememberSpokenText(text: string, nowMs = Date.now()): void {
  const normalized = normalizeText(text);
  if (normalized === '') {
    return;
  }
  const records = readRecords().filter((record) => nowMs - record.at < DEDUPE_WINDOW_MS);
  records.push({ text: normalized, at: nowMs });
  writeRecords(records);
}

export { nextIdempotencyKey, resetIdempotencyKey } from '../services/idempotency';
