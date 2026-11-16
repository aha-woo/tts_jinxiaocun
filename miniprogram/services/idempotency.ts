/**
 * 幂等键（端侧）——防「连点两次落两笔账」。
 *
 * 服务端行为见 `server/app/common/idempotency.py`：
 * 首次请求执行并缓存结果 60s，同 key 重复请求直接复用首次结果；
 * 处理中的重复请求返回 `E_DUPLICATE_REQUEST`。
 *
 * 端侧策略：同一「作用域」在 60s 内复用同一个 key（用户连点/网络重试都安全）；
 * 作用域变化（换接口或显式 reset）才生成新 key。
 */

/** key 复用窗口：与服务端 TTL 对齐（60s 去重口径）。 */
export const IDEMPOTENCY_TTL_MS = 60_000;

interface KeyEntry {
  key: string;
  expiresAt: number;
}

const entries = new Map<string, KeyEntry>();
let sequence = 0;

function randomSuffix(): string {
  // 小程序无 crypto.randomUUID，用时间戳 + 递增序号 + 随机数拼装，避免碰撞
  sequence += 1;
  const random = Math.floor(Math.random() * 0xffffff)
    .toString(16)
    .padStart(6, '0');
  return `${Date.now().toString(36)}-${sequence.toString(36)}-${random}`;
}

/**
 * 取（或新建）作用域对应的幂等键。
 *
 * @param scope 作用域，建议 `METHOD:/path` 或 `card:{cardId}`
 */
export function nextIdempotencyKey(scope: string): string {
  const now = Date.now();
  const existing = entries.get(scope);
  if (existing !== undefined && existing.expiresAt > now) {
    return existing.key;
  }
  const key = `${scope}#${randomSuffix()}`.replace(/\s+/g, '');
  entries.set(scope, { key, expiresAt: now + IDEMPOTENCY_TTL_MS });
  return key;
}

/** 查看作用域当前 key（不创建）。 */
export function peekIdempotencyKey(scope: string): string | null {
  const existing = entries.get(scope);
  if (existing === undefined || existing.expiresAt <= Date.now()) {
    return null;
  }
  return existing.key;
}

/** 业务成功落账后清理，避免下一次同样操作被当成重放。 */
export function resetIdempotencyKey(scope: string): void {
  entries.delete(scope);
}

/** 清空全部（退出登录 / 切换门店时调用）。 */
export function clearIdempotencyKeys(): void {
  entries.clear();
}

/** 清理过期条目（页面 onShow 可调，避免长期驻留内存）。 */
export function pruneExpiredKeys(): void {
  const now = Date.now();
  entries.forEach((entry, scope) => {
    if (entry.expiresAt <= now) {
      entries.delete(scope);
    }
  });
}
