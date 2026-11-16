/**
 * 后端调用封装：**唯一**出网入口。
 *
 * 职责
 * ----
 * 1. 走微信云托管 `callContainer` 私有协议（免域名 / 免 ICP 备案 / 免 HTTPS 证书）；
 * 2. 统一解包 `{code,data,message}`：`code !== 0` 抛 {@link ApiError}；
 * 3. 写请求自动带 `Idempotency-Key`（防连点重复落账，见 services/idempotency.ts）；
 * 4. 本地联调可切 `wx.request`（仅开发态，生产必须走 callContainer）。
 *
 * 契约来源：`packages/shared/contracts/api-envelope.ts`（单一真相源，
 * 端侧此处按其结构声明类型，字段语义必须保持一致）。
 */

import { nextIdempotencyKey } from './idempotency';

/** 云开发环境 id：部署时替换为真实环境（不要提交真实值到公开仓库）。 */
export const CLOUD_ENV = 'REPLACE_WITH_CLOUD_ENV_ID';

/** 云托管服务名，对应 callContainer 请求头 `X-WX-SERVICE`。 */
export const CLOUD_SERVICE = 'voice-inventory-api';

/** 本地联调地址：留空则走云托管；填 `http://127.0.0.1:8000` 走 wx.request。 */
export const LOCAL_BASE_URL = '';

/** 成功码，与 `packages/shared/contracts/api-envelope.ts` 的 SUCCESS_CODE 一致。 */
export const SUCCESS_CODE = 0;

const DEFAULT_TIMEOUT_MS = 8000;

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** 统一响应包络。 */
export interface ApiEnvelope<T = unknown> {
  code: number;
  data: T;
  message: string;
}

/** 健康检查返回结构（T01 唯一已实现接口）。 */
export interface HealthPayload {
  status: 'ok' | 'degraded';
  version: string;
  env: string;
  checked_at: string;
  contract: {
    intents: number;
    slots: number;
    sources: number;
    error_codes: number;
    domains: number;
  };
  idempotency: {
    backend: string;
    degraded?: boolean;
  };
  capabilities: {
    call_container: boolean;
    max_request_body_bytes: number;
    web_client: boolean;
    memo: string;
  };
}

/** 业务错误：`errorCode` 为字符串码（如 E_R3_PC_REQUIRED），`code` 为数值码。 */
export class ApiError extends Error {
  readonly code: number;

  readonly errorCode: string;

  readonly data: unknown;

  constructor(code: number, message: string, errorCode = '', data: unknown = null) {
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

export interface RequestOptions {
  method?: HttpMethod;
  data?: Record<string, unknown> | undefined;
  /** 显式指定幂等键（写接口必须唯一且可复用）；不传则由 idempotency 模块生成 */
  idempotencyKey?: string | undefined;
  /** 幂等键作用域（默认 method + path） */
  idempotencyScope?: string | undefined;
  timeoutMs?: number | undefined;
}

let cloudReadyPromise: Promise<void> | null = null;

/**
 * 初始化云能力（幂等，可重复调用）。
 *
 * 注意：`callContainer` 要求基础库 >= 2.23.0（见 project.config.json libVersion）。
 */
export function ensureCloudReady(): Promise<void> {
  if (cloudReadyPromise !== null) {
    return cloudReadyPromise;
  }
  cloudReadyPromise = new Promise<void>((resolve, reject) => {
    if (typeof wx === 'undefined' || wx.cloud === undefined) {
      reject(new Error('当前基础库不支持 wx.cloud，请升级微信或基础库'));
      return;
    }
    try {
      wx.cloud.init({ env: CLOUD_ENV, traceUser: true });
      resolve();
    } catch (error) {
      cloudReadyPromise = null;
      reject(error instanceof Error ? error : new Error(String(error)));
    }
  });
  return cloudReadyPromise;
}

/**
 * 组装请求头；写请求自动带 `Idempotency-Key`（作用域默认 `METHOD:/path`）。
 */
function buildHeaders(path: string, options: RequestOptions & { method: HttpMethod }): Record<string, string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    'X-WX-SERVICE': CLOUD_SERVICE,
  };
  if (options.method !== 'GET') {
    const scope = options.idempotencyScope ?? `${options.method}:${path}`;
    headers['Idempotency-Key'] = options.idempotencyKey ?? nextIdempotencyKey(scope);
  }
  return headers;
}

function envelopeOf(raw: unknown): ApiEnvelope<unknown> | null {
  if (typeof raw !== 'object' || raw === null) {
    return null;
  }
  const candidate = raw as Record<string, unknown>;
  if (typeof candidate.code !== 'number' || typeof candidate.message !== 'string') {
    return null;
  }
  return {
    code: candidate.code,
    data: candidate.data,
    message: candidate.message,
  };
}

function unwrap<T>(raw: unknown, httpStatus: number): T {
  const envelope = envelopeOf(raw);
  if (envelope === null) {
    throw new ApiError(-1, `响应格式不正确（HTTP ${httpStatus}）`, '', raw);
  }
  if (envelope.code === SUCCESS_CODE) {
    return envelope.data as T;
  }
  const data = envelope.data as Record<string, unknown> | null;
  const errorCode = data !== null && typeof data === 'object' ? String(data.error_code ?? '') : '';
  throw new ApiError(envelope.code, envelope.message, errorCode, envelope.data);
}

/** 走云托管私有协议。 */
async function callViaContainer(path: string, options: RequestOptions): Promise<unknown> {
  await ensureCloudReady();
  const method: HttpMethod = options.method ?? 'GET';
  return new Promise<unknown>((resolve, reject) => {
    wx.cloud.callContainer({
      config: { env: CLOUD_ENV },
      path,
      method,
      header: buildHeaders(path, { ...options, method }),
      data: options.data ?? {},
      timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      success: (res: { statusCode: number; data: unknown }) => {
        try {
          resolve(unwrap(res.data, res.statusCode));
        } catch (error) {
          reject(error);
        }
      },
      fail: (error: { errMsg?: string }) => {
        reject(new ApiError(-2, `网络异常：${error.errMsg ?? '未知错误'}`));
      },
    });
  });
}

/** 仅本地联调：直接 http 打到 uvicorn。 */
async function callViaHttp(path: string, options: RequestOptions): Promise<unknown> {
  const method: HttpMethod = options.method ?? 'GET';
  return new Promise<unknown>((resolve, reject) => {
    wx.request({
      url: `${LOCAL_BASE_URL}${path}`,
      // 微信 wx.request 的类型声明未包含 PATCH（平台限制），本地联调时按请求头透传
      method: method as 'GET' | 'POST' | 'PUT' | 'DELETE',
      header: buildHeaders(path, { ...options, method }),
      data: options.data ?? {},
      timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      success: (res: { statusCode: number; data: unknown }) => {
        try {
          resolve(unwrap(res.data, res.statusCode));
        } catch (error) {
          reject(error);
        }
      },
      fail: (error: { errMsg?: string }) => {
        reject(new ApiError(-2, `网络异常：${error.errMsg ?? '未知错误'}`));
      },
    });
  });
}

/**
 * 调用后端接口并解包。
 *
 * @param path 以 `/` 开头的接口路径，如 `/healthz`
 * @param options 请求选项（写接口请传稳定的 idempotencyKey）
 * @returns 解包后的业务数据
 * @throws ApiError 业务失败或网络异常
 */
export async function callApi<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const raw =
    LOCAL_BASE_URL === ''
      ? await callViaContainer(normalizedPath, options)
      : await callViaHttp(normalizedPath, options);
  return raw as T;
}

/** 健康检查（首页展示后端状态 + 契约计数）。 */
export function health(): Promise<HealthPayload> {
  return callApi<HealthPayload>('/healthz', { method: 'GET', timeoutMs: 5000 });
}
