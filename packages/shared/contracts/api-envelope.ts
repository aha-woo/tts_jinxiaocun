/**
 * 统一 API 响应包络（端侧唯一解包入口）。
 *
 * 约定来源：docs/02-架构设计.md §8.4「API 响应包络」——
 * 统一 `{code: int, data: any, message: str}`，`code = 0` 表示成功。
 *
 * 注意：`code` 是**数值**（见 error-codes.json 的 `numeric` 字段），
 * 字符串常量 `E_*` 只用于日志与埋点，不直接作为包络 code。
 */

/** 成功包络的 code 值。 */
export const SUCCESS_CODE = 0;

/** 服务端统一响应包络。 */
export interface ApiEnvelope<T = unknown> {
  code: number;
  data: T;
  message: string;
}

/** 业务错误（由 services/api.ts 在 code !== 0 时抛出）。 */
export class ApiError extends Error {
  /** 数值错误码（error-codes.json 的 numeric）。 */
  readonly code: number;
  /** 可选字符串错误码（如 'E_R3_PC_REQUIRED'），来自响应头或 data 透出。 */
  readonly errorCode: string | null;
  /** 服务端返回的 data（通常是错误详情）。 */
  readonly data: unknown;

  constructor(code: number, message: string, errorCode: string | null = null, data: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.errorCode = errorCode;
    this.data = data;
  }

  /** 是否为指定字符串错误码。 */
  is(errorCode: string): boolean {
    return this.errorCode === errorCode;
  }
}

/** 构造成功包络（测试与本地 mock 使用）。 */
export function ok<T>(data: T, message = 'ok'): ApiEnvelope<T> {
  return { code: SUCCESS_CODE, data, message };
}

/** 构造失败包络。 */
export function fail<T = null>(numericCode: number, message: string, data: T | null = null): ApiEnvelope<T | null> {
  return { code: numericCode, data, message };
}

/** 类型守卫：判断响应体是否为合法包络。 */
export function isApiEnvelope(value: unknown): value is ApiEnvelope {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const v = value as Record<string, unknown>;
  return typeof v.code === 'number' && typeof v.message === 'string' && 'data' in v;
}

/**
 * 解包：code=0 返回 data，否则抛 ApiError。
 *
 * @param envelope 服务端原始响应体
 * @param httpStatus HTTP 状态码（仅用于错误信息兜底）
 */
export function unwrap<T>(envelope: unknown, httpStatus = 200): T {
  if (!isApiEnvelope(envelope)) {
    throw new ApiError(-1, `响应不是合法包络（HTTP ${httpStatus}）`, null, envelope);
  }
  if (envelope.code === SUCCESS_CODE) {
    return envelope.data as T;
  }
  const errorCode =
    typeof envelope.data === 'object' && envelope.data !== null && 'error_code' in (envelope.data as object)
      ? String((envelope.data as Record<string, unknown>)['error_code'] ?? '')
      : null;
  throw new ApiError(envelope.code, envelope.message || '请求失败', errorCode || null, envelope.data);
}

/** 健康检查响应（T01 唯一已实现的接口）。 */
export interface HealthPayload {
  status: 'ok' | 'degraded';
  version: string;
  contract: {
    intents: number;
    slots: number;
    sources: number;
    error_codes: number;
    domains: number;
  };
}
