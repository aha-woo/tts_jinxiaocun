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

let lastScope = '';
let lastWindow = 0;
let lastKey = '';

/**
 * 生成幂等键（同一作用域 + 同一 60s 窗口内**稳定**）。
 *
 * ⚠️ 语义提醒：稳定是刻意的——用户点「确认」两下必须复用同一个键，
 * 服务端才能识别成同一次操作；换一张卡片（card_id 变了）作用域自然就变了。
 *
 * @param scope 作用域（如 `POST:/api/v1/voice/cards/x/confirm`）
 * @param nowMs 当前时间戳（注入便于测试）
 * @returns 幂等键字符串
 */
export function nextIdempotencyKey(scope: string, nowMs = Date.now()): string {
  const window = Math.floor(nowMs / DEDUPE_WINDOW_MS);
  if (scope === lastScope && window === lastWindow && lastKey !== '') {
    return lastKey;
  }
  lastScope = scope;
  lastWindow = window;
  lastKey = `${scope}#${window}`;
  return lastKey;
}

/** 强制换一个幂等键（用户明确要「重新提交」时用）。 */
export function resetIdempotencyKey(): void {
  lastScope = '';
  lastWindow = 0;
  lastKey = '';
}
