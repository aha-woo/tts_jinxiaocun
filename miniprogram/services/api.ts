/**
 * 后端调用封装：**唯一**出网入口。
 *
 * 职责
 * ----
 * 1. 生产**只**走微信云托管 `callContainer` 私有协议（免域名 / 免 ICP 备案 / 免 HTTPS 证书）；
 * 2. 统一解包 `{code,data,message}`：`code !== 0` 抛 {@link ApiError}；
 * 3. 写请求自动带 `Idempotency-Key`（防连点重复落账，见 utils/dedupe.ts）；
 * 4. 本地联调走 `services/dev-channel.ts`（**该文件独占了所有 `http://` 与 `wx.request`**，
 *    本文件里一个都没有——这样「生产不存在 http 直连」是可机械检查的，不是口头保证）。
 *
 * 契约来源：`packages/shared/contracts/api-envelope.ts`（单一真相源）。
 * 下面的接口类型**逐字段对齐** `server/app/api/v1/*` 的真实返回结构，
 * 不写「差不多」的类型——类型对不上就等于联调没做。
 */

import { DEV_MODE, devCall } from './dev-channel';
import { nextIdempotencyKey, resetIdempotencyKey } from './idempotency';

/**
 * 云开发环境 id：**云托管服务所在的那个环境**（不是云开发数据库所在环境也一样）。
 *
 * 2026-11-19：切到新建的 PG 环境 `ttsstorage`（上海 / PostgreSQL 模式），
 * 云托管与数据库都放在这个环境里；换环境只改这一行。
 * （旧的 `cloud1` 留着跑别的云函数，不再承载本项目后端。）
 */
export const CLOUD_ENV = 'ttsstorage-d0gqei1fhbec8cf2a';

/** 云托管服务名，对应 callContainer 请求头 `X-WX-SERVICE`。 */
export const CLOUD_SERVICE = 'voice-inventory-api';

/** 成功码，与 `packages/shared/contracts/api-envelope.ts` 的 SUCCESS_CODE 一致。 */
export const SUCCESS_CODE = 0;

const DEFAULT_TIMEOUT_MS = 15000;
const TOKEN_STORAGE_KEY = 'vi.token';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** 统一响应包络。 */
export interface ApiEnvelope<T = unknown> {
  code: number;
  data: T;
  message: string;
}

/** 健康检查返回结构。 */
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
  idempotency: { backend: string; degraded?: boolean };
  capabilities: {
    call_container: boolean;
    max_request_body_bytes: number;
    web_client: boolean;
    memo: string;
  };
}

/**
 * 登录返回（`server/app/api/v1/auth.py::login` 的真实字段）。
 *
 * 注意字段名是 ``jwt`` 而不是 ``access_token``：**按真实返回写类型**，
 * 不按想象写——类型对不上就等于联调没做（这一条已经踩过一次）。
 */
export interface LoginPayload {
  jwt: string;
  token_type: string;
  expires_at: string;
  tenant_id: string;
  store_id: string;
  member_id: string;
  role: string;
  tenant_version: number;
  plan: string;
}

/** 槽位来源角标（5 值枚举，与 `source-enum.json` 一致）。 */
export type SourceTag = 'asr' | 'default' | 'computed' | 'alias' | 'pending';

/** 卡片 ② 区一行（`CardSlotView.as_dict()`）。 */
export interface CardSlot {
  code: string;
  label_zh: string;
  value: unknown;
  display: string;
  source: SourceTag;
  tag: string | null;
  tone: string;
  editable: boolean;
  calc_trace: string | null;
}

/** 卡片 ③ 区一行明细（`CardLineView.as_dict()`，多商品展开成 N 行）。 */
export interface CardLine {
  product_label: string;
  quantity: unknown;
  unit: string;
  amount_fen: number;
  amount_display: string;
  unit_price_fen: number | null;
  qty_base: unknown;
  pending: boolean;
  source_map: Record<string, string>;
  calc_trace: string | null;
  product_id?: string;
  similar_products?: { product_id: string; name: string }[];
}

/** 卡片 ④ 区一行影响预演（`PreviewItem.as_dict()`）。 */
export interface CardImpact {
  entity: string;
  key: string;
  label: string;
  before: string;
  delta: string;
  after: string;
  unit: string;
  version: number;
  kind: string;
  warnings: string[];
  calc_trace: string | null;
  pending: boolean;
}

/** 卡片 ⑤ 区风险条（`RiskBanner.as_dict()`）。 */
export interface CardRisk {
  level: string;
  reason_zh: string;
  conflicts: string[];
}

/** 卡片 ⑦ 区一个按钮（`CardAction.as_dict()`）。 */
export interface CardAction {
  key: string;
  label: string;
  kind: string;
  requires_voice: boolean;
  button_fallback: boolean;
}

/** 确认卡片：**7 区块** + 落账元信息（`ConfirmCardView.to_payload()`）。 */
export interface CardPayload {
  /** ① 原文区 */
  original: { asr_text: string; asr_confidence: number; low_confidence_highlight: boolean };
  /** ② 意图区 */
  intent: {
    intent_id: string;
    intent_label_zh: string;
    domain: string;
    domain_label_zh: string;
    risk_level: string;
    confirm_policy: 'C1' | 'C2';
  };
  /** ③ 槽位区 */
  slots: CardSlot[];
  /** ④ 计算区 */
  calc: { calc_traces: string[]; lines: CardLine[] };
  /** ⑤ 影响预览区 */
  impact: CardImpact[];
  /** ⑥ 风险区 */
  risk: CardRisk;
  /** ⑦ 操作区 */
  actions: { primary_label: string; actions: CardAction[] };
  /** 落账元信息（CAS 版本、30s 截止时刻） */
  meta: {
    card_id: string;
    session_id: string;
    preview_version: number;
    expires_at: string | null;
    route: string;
    warnings: string[];
  };
  /** 扣次信息（只在建卡响应里有） */
  billing?: { billed: boolean; duplicated: boolean; detail: string };
  /** 待补缺口（服务端 gaps；空数组 = 没有缺项） */
  gaps?: PendingGap[];
}

/** 澄清追问。 */
export interface ClarifyPayload {
  slot: string;
  question: string;
  options: string[];
}

/** 创建会话的返回（含上传票据；**音频不进请求体**）。 */
export interface SessionPayload {
  session_id: string | null;
  state: string;
  message_zh?: string;
  audio_object_ref?: string;
  upload_ticket?: {
    ticket_id: string;
    audio_object_ref: string;
    upload_prefix: string;
    expires_at: string;
    max_bytes: number;
  };
  quota?: { remaining_minute: number; scope: string };
  degrade_level: string;
  note?: string;
}

/** 新手期进度与毕业状态。 */
export interface NoviceStatus {
  count: number;
  first_pass_count: number;
  target: number;
  first_pass_rate: number;
  in_novice: boolean;
  graduated: boolean;
  extended: boolean;
  auto_post_enabled: boolean;
  just_graduated: boolean;
}

/** 解析返回（结构随 route 变化，下面把各分支的字段都声明成可选）。 */
export interface ParsePayload {
  session_id: string;
  stage: string;
  reason: string;
  asr_text: string;
  asr_confidence: number;
  corrections: number;
  rule_hit: boolean;
  llm_calls: number;
  trace: string[];
  parse_result: {
    intent: string;
    slots: Record<string, unknown>;
    source: Record<string, string>;
    confidence: number;
    clarify: ClarifyPayload | null;
    route: string | null;
    calc_traces: string[];
  } | null;
  route: string;
  /** route=static */
  static_answer?: string;
  /** route=clarify */
  clarify?: ClarifyPayload | null;
  /** route=answer（R0 只读查询结果） */
  answer?: {
    kind: string;
    items?: unknown[];
    report?: Record<string, unknown>;
    cost_hidden?: boolean;
    note?: string;
  } | null;
  /** route=card / auto_posted / card_updated */
  card?: CardPayload | null;
  auto_posted?: boolean;
  result?: ConfirmPayload;
  novice?: NoviceStatus;
  undo_window_seconds?: number;
  toast_zh?: string;
  learned_rule?: { from: string; to: string };
  credit_consumed?: boolean;
  /** route=rejected / 失败 */
  error_code?: string;
  /** route=pending_task（R3） */
  task?: { task_id: string; status: string } | null;
  degrade_level?: string;
}

/** 确认落账结果（`ConfirmOutcome.as_dict()`）。 */
export interface ConfirmPayload {
  card_id: string;
  doc_id: string | null;
  doc_no: string | null;
  card_status: string;
  confirmed: boolean;
  total_amount_fen: number;
  inventory_after: Record<string, string>;
  reversal_deadline: string | null;
  expired: boolean;
  needs_second_voice: boolean;
  error_code: string | null;
  detail: string;
  novice?: NoviceStatus;
}

/** 落账接口的包络体（`app/api/v1/confirm.py` 返回 `{result, idempotency_key}`）。 */
export interface ConfirmEnvelope {
  result: ConfirmPayload;
  idempotency_key: string;
}

/** 二次确认的包络体（`app/api/v1/voice.py::confirm2` 返回 `{result, note}`）。 */
export interface Confirm2Envelope {
  result: ConfirmPayload;
  note: string;
}

/**
 * 澄清点选的返回（`app/api/v1/clarify.py`）。
 *
 * **不是** `ParsePayload`：点选只做本地结构化更新（0 次模型调用），
 * 所以不会有 `asr_text` / `llm_calls` 这些字段。
 */
export interface ClarifyAnswerPayload {
  route: 'clarify' | 'resolved' | 'manual';
  round_used: number;
  max_rounds: number;
  slots: Record<string, unknown>;
  source: Record<string, string>;
  clarify: ClarifyPayload | null;
  finished: boolean;
  credit_consumed: boolean;
  note: string;
  /** route=manual 时才有 */
  error_code?: string;
  manual_entry_required?: boolean;
  message_zh?: string;
}

/** 只读查询（R0）：所有 `/api/v1/query/*` 都带 `readonly` 与 `billed` 两个标记。 */
export interface QueryPayload<T = unknown> {
  items: T[];
  readonly: boolean;
  billed: boolean;
  /** 欠款查询专有 */
  total_outstanding_fen?: number;
  /** 报表查询专有 */
  report?: Record<string, unknown>;
  top_products?: unknown[];
}

/** 用量（**自然月**口径，与权益的 30 天滚动窗口分开）。 */
export interface UsagePayload {
  period: string;
  used: number;
  used_count?: number;
  quota: number;
  free_quota?: number;
  remaining?: number;
  cost_fen?: number;
  degrade_level: string;
  caliber: string;
  [key: string]: unknown;
}

/** `/api/v1/usage` 的包络体。 */
export interface UsageEnvelope {
  usage: UsagePayload;
  ledger_rows: number;
  caliber_note: string;
}

/** 权益（**30 天滚动窗口**口径）。 */
export interface EntitlementPayload {
  plan_code: string;
  plan_name: string;
  valid_until: string | null;
  valid_until_display?: string;
  days_left: number;
  remaining_quota: number;
  remaining?: number;
  caliber: string;
  auto_renew: boolean;
  [key: string]: unknown;
}

/** `/api/v1/entitlement` 的包络体（权益 + 用量两份口径并列，**不许拼进同一个进度条**）。 */
export interface EntitlementEnvelope {
  entitlement: EntitlementPayload;
  usage: UsagePayload;
  caliber_note: string;
}

/** 道具（价格来自 `goods.json`，端上不硬编码价格）。 */
export interface GoodsItem {
  goods_id: string;
  goods_type?: string;
  name: string;
  name_zh?: string;
  price_fen: number;
  price_text: string;
  desc: string;
  benefits_zh?: string;
  quota_amount?: number;
  validity_days?: number;
  [key: string]: unknown;
}

/** `/api/v1/entitlement/goods` 的返回。 */
export interface GoodsPayload {
  items: GoodsItem[];
  channel: string;
  forbidden_channels: string[];
  min_payment_fen: number;
  auto_renew: boolean;
  note: string;
}

/** `/api/v1/entitlement/order` 的返回（**前端 success 不发货**）。 */
export interface OrderPayload {
  order: {
    out_trade_no: string;
    goods_id: string;
    price_fen: number;
    pay_params: Record<string, string>;
    [key: string]: unknown;
  };
  deliver_rule: string;
}

/** R3 待办一行（`PendingTask.as_dict()`）。 */
export interface PendingTaskRow {
  task_id: string;
  session_id: string;
  intent_id: string;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
  requires_pc: boolean;
  required_role: string;
  hint_zh: string;
}

/** 审批结果（`TaskDecision.as_dict()`）。 */
export interface TaskDecisionPayload {
  task_id: string;
  status: string;
  approved: boolean;
  executed: boolean;
  detail: string;
  [key: string]: unknown;
}

/** 导出任务一行（`ExportJob.as_dict()`）。 */
export interface ExportJobRow {
  job_id: string;
  status: string;
  scope: string;
  file_format: string;
  file_ref: string | null;
  signed_url: string | null;
  signed_url_expires_at: string | null;
  idempotent: boolean;
  row_count: number;
  delivery: string;
  detail: string;
}

/** 个人词表一行（`GET /api/v1/dict`）。 */
export interface DictRow {
  dict_id: string;
  raw_text: string;
  mapped_type: string;
  mapped_type_label?: string;
  mapped_id: string | null;
  mapped_name?: string | null;
  layer: string;
  version: number;
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
  /** 显式指定幂等键（写接口必须唯一且可复用）；不传则由 dedupe 模块生成 */
  idempotencyKey?: string | undefined;
  /** 幂等键作用域（默认 method + path） */
  idempotencyScope?: string | undefined;
  timeoutMs?: number | undefined;
}

let cloudReadyPromise: Promise<void> | null = null;

/** 初始化云能力（幂等，可重复调用）。`callContainer` 要求基础库 >= 2.23.0。 */
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

/** 读本地 JWT（微信登录态换来的；**不做账号密码**）。 */
export function getToken(): string {
  try {
    return String(wx.getStorageSync(TOKEN_STORAGE_KEY) ?? '');
  } catch (error) {
    console.warn('[api] 读 token 失败', error);
    return '';
  }
}

/** 写本地 JWT。 */
export function setToken(token: string): void {
  try {
    wx.setStorageSync(TOKEN_STORAGE_KEY, token);
  } catch (error) {
    console.warn('[api] 写 token 失败', error);
  }
}

/** 组装请求头；写请求自动带 `Idempotency-Key`（作用域默认 `METHOD:/path`）。 */
function buildHeaders(
  path: string,
  options: RequestOptions & { method: HttpMethod }
): Record<string, string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    'X-WX-SERVICE': CLOUD_SERVICE,
  };
  const token = getToken();
  if (token !== '') {
    headers.Authorization = `Bearer ${token}`;
  }
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
  return { code: candidate.code, data: candidate.data, message: candidate.message };
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

/** 走云托管私有协议（**生产唯一出网路径**）。 */
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

/**
 * 调用后端接口并解包。
 *
 * @param path 以 `/` 开头的接口路径，如 `/healthz`
 * @param options 请求选项（写接口请传稳定的 idempotencyKey）
 * @returns 解包后的业务数据
 * @throws ApiError 业务失败或网络异常
 */
/** 清掉本地 token（登录过期后重登前调用）。 */
let tokenClearHook: (() => void) | null = null;

export function registerTokenClearHook(hook: () => void): void {
  tokenClearHook = hook;
}

export function clearToken(): void {
  try {
    wx.removeStorageSync(TOKEN_STORAGE_KEY);
  } catch (error) {
    console.warn('[api] 清 token 失败', error);
  }
  tokenClearHook?.();
}

let reloginPending: Promise<void> | null = null;

/** 登录过期时静默重登一次（并发安全，只发一次 wx.login）。 */
function reloginOnce(): Promise<void> {
  if (reloginPending !== null) {
    return reloginPending;
  }
  reloginPending = new Promise<void>((resolve, reject) => {
    wx.login({
      success: (res: { code: string }) => {
        login(res.code)
          .then(() => resolve())
          .catch((error: unknown) => reject(error));
      },
      fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '登录失败')),
    });
  }).then(
    () => {
      reloginPending = null;
    },
    (error: unknown) => {
      reloginPending = null;
      throw error;
    }
  );
  return reloginPending;
}

function isAuthExpired(error: unknown): boolean {
  return error instanceof ApiError && error.errorCode === 'E_AUTH_INVALID';
}

function isTransientNetworkError(error: unknown): boolean {
  return error instanceof ApiError && error.code === -2;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function doCallApi<T>(path: string, options: RequestOptions): Promise<T> {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const method: HttpMethod = options.method ?? 'GET';

  if (DEV_MODE) {
    // 只有 dev-channel 里的开关为 true 才会进这里；生产构建里它恒为 false
    const response = await devCall({
      path: normalizedPath,
      method,
      header: buildHeaders(normalizedPath, { ...options, method }),
      data: options.data ?? {},
      timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    });
    return unwrap<T>(response.data, response.statusCode);
  }

  const raw = await callViaContainer(normalizedPath, options);
  return raw as T;
}

/**
 * 调用后端接口并解包。
 *
 * 1. 遇到微信云托管瞬时断连/超时（`code === -2`）：等待 500ms 自动重试一次
 *    （写接口复用同一 `Idempotency-Key`，后端幂等表防重入，绝不重复落账）；
 * 2. 登录过期（`E_AUTH_INVALID`）：清 token → 静默重登一次 → 原请求重试一次。
 */
export async function callApi<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method: HttpMethod = options.method ?? 'GET';
  const scope = options.idempotencyScope ?? `${method}:${path}`;
  try {
    const result = await doCallApi<T>(path, options);
    if (method !== 'GET' && options.idempotencyKey === undefined) {
      resetIdempotencyKey(scope);
    }
    return result;
  } catch (error) {
    if (isTransientNetworkError(error)) {
      await delay(500);
      try {
        const retried = await doCallApi<T>(path, options);
        if (method !== 'GET' && options.idempotencyKey === undefined) {
          resetIdempotencyKey(scope);
        }
        return retried;
      } catch (retryError) {
        if (method !== 'GET' && options.idempotencyKey === undefined) {
          resetIdempotencyKey(scope);
        }
        if (isTransientNetworkError(retryError)) {
          throw new ApiError(-2, '网络连接不稳定，请稍后下拉刷新重试');
        }
        throw retryError;
      }
    }
    if (isAuthExpired(error) && !path.includes('/auth/login')) {
      clearToken();
      await reloginOnce();
      const retried = await doCallApi<T>(path, options);
      if (method !== 'GET' && options.idempotencyKey === undefined) {
        resetIdempotencyKey(scope);
      }
      return retried;
    }
    if (method !== 'GET' && options.idempotencyKey === undefined) {
      resetIdempotencyKey(scope);
    }
    throw error;
  }
}

/** 健康检查（首页状态条用）。 */
export function health(): Promise<HealthPayload> {
  return callApi<HealthPayload>('/healthz', { method: 'GET', timeoutMs: 5000 });
}

/** 微信登录：code 换 JWT。 */
export async function login(code: string): Promise<LoginPayload> {
  const payload = await callApi<LoginPayload>('/api/v1/auth/login', {
    method: 'POST',
    data: { code },
    idempotencyKey: `login:${code}`,
  });
  setToken(payload.jwt);
  return payload;
}

/** 申请音频上传票据（只收引用，音频字节不走业务接口）。 */
export function issueUploadTicket(body: {
  session_id?: string;
  duration_ms: number;
  client_bytes?: number;
  codec?: string;
}): Promise<{
  ticket_id: string;
  audio_object_ref: string;
  upload_prefix: string;
  expires_at: string;
  max_bytes: number;
}> {
  return callApi('/api/v1/upload/ticket', {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
  });
}

/** 创建语音会话（门禁 + 上传票据 + 令牌桶；**无音频字段**）。 */
export function createVoiceSession(body: {
  duration_ms: number;
  mode: 'tap' | 'hold';
  /** voice=按住说（过录音门禁）；manual=手动记账（无音频，服务端跳过门禁） */
  source?: 'voice' | 'manual';
  energy_rms?: number;
  vad_tail_ms?: number;
}): Promise<SessionPayload> {
  return callApi<SessionPayload>('/api/v1/voice/sessions', {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    timeoutMs: 8000,
  });
}

/** 解析（只传 `audio_object_ref` 引用或已转好的文本）。 */
export function parseSession(
  sessionId: string,
  body: {
    audio_object_ref?: string;
    asr_text?: string;
    asr_confidence?: number;
    duration_ms?: number;
    active_card_id?: string;
  }
): Promise<ParsePayload> {
  return callApi<ParsePayload>(`/api/v1/voice/sessions/${sessionId}/parse`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    timeoutMs: 12000,
  });
}

/** 澄清点选（**不重调 AI**，纯本地结构化更新）。 */
export function answerClarify(
  sessionId: string,
  body: { slot: string; value: string }
): Promise<ClarifyAnswerPayload> {
  return callApi<ClarifyAnswerPayload>(`/api/v1/voice/sessions/${sessionId}/clarify`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
  });
}

/** C2 二次确认（语音或按钮兜底；**15s 超时不执行**）。 */
export function confirm2(
  sessionId: string,
  body: { spoken_text?: string; button?: boolean }
): Promise<Confirm2Envelope> {
  return callApi<Confirm2Envelope>(`/api/v1/voice/sessions/${sessionId}/confirm2`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyKey: `confirm2:${sessionId}`,
  });
}

/** 取卡片（含 7 区块）。 */
export function getCard(cardId: string): Promise<CardPayload> {
  return callApi<CardPayload>(`/api/v1/voice/cards/${cardId}`, { method: 'GET' });
}

/** 确认落账（**唯一写入口**）。返回 `{result, idempotency_key}`。 */
export function confirmCard(
  cardId: string,
  body: { confirm_type: 'button' | 'voice2'; button_fallback?: boolean; spoken_text?: string },
  idempotencyKeyOverride?: string
): Promise<ConfirmEnvelope> {
  return callApi<ConfirmEnvelope>(`/api/v1/voice/cards/${cardId}/confirm`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyKey: idempotencyKeyOverride ?? `confirm:${cardId}`,
  });
}

/** 取消卡片（不落账；扣次发生在建卡成功时，**不退款**）。 */
export function cancelCard(
  cardId: string
): Promise<{ card_id: string; status: string; credit_refunded: boolean; note: string }> {
  return callApi(`/api/v1/voice/cards/${cardId}/cancel`, {
    method: 'POST',
    idempotencyKey: `cancel:${cardId}`,
  });
}

/** 编辑卡片槽位（**本地结构化重算，不重调 AI**）。服务端收 `{ slots: [{code,value}] }`。 */
export function editCard(
  cardId: string,
  slots: { code: string; value: string | number }[]
): Promise<CardPayload & { llm_calls: number; note: string; toast_zh?: string; learned_rule?: { from: string; to: string } }> {
  return callApi(`/api/v1/voice/cards/${cardId}/edit`, {
    method: 'PATCH',
    data: { slots } as unknown as Record<string, unknown>,
  });
}

/** 单字段语音重试（0 次大模型调用）。 */
export function fieldVoiceCard(
  cardId: string,
  body: { slot_code: string; spoken_text: string }
): Promise<CardPayload & { llm_calls: number; parsed_updates: Record<string, unknown>; toast_zh?: string; note: string }> {
  return callApi(`/api/v1/voice/cards/${cardId}/field-voice`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
  });
}

/** 「要补」的一项：code 直接给 PATCH 编辑用，label/unit 直接上屏。 */
export interface PendingGap {
  code: string;
  label: string;
  unit: string;
  must: boolean;
  current: string;
}

/** 待确认列表项：服务端只给摘要（card_id / session_id / intent_id / status / asr_text / gaps），
 *  **不是**完整 7 区块卡片，金额与明细要打开后再取。 */
export interface PendingCardRow {
  card_id: string;
  session_id: string;
  intent_id: string;
  status: string;
  preview_version: number;
  asr_text: string;
  billed: boolean;
  note?: string;
  /** 还缺哪些项；空数组 = 只差确认 */
  gaps?: PendingGap[];
}

/** 复活超时卡片（复用同一 session，不重复扣次）。 */
export function reviveCard(cardId: string): Promise<CardPayload> {
  return callApi(`/api/v1/voice/cards/${cardId}/revive`, { method: 'POST' });
}

/** 待确认卡片列表。 */
export function listPending(): Promise<{ items: PendingCardRow[]; note: string }> {
  return callApi('/api/v1/voice/cards/pending', { method: 'GET' });
}

/** 只读查询：库存（R0，不建卡、不扣次）。 */
export function queryStock(name = '', lowOnly = false, limit?: number): Promise<QueryPayload> {
  const params: string[] = [];
  if (name !== '') {
    params.push(`name=${encodeURIComponent(name)}`);
  }
  if (lowOnly) {
    params.push('low_only=true');
  }
  if (limit !== undefined && limit > 0) {
    params.push(`limit=${Math.round(limit)}`);
  }
  const suffix = params.length > 0 ? `?${params.join('&')}` : '';
  return callApi(`/api/v1/query/stock${suffix}`, { method: 'GET' });
}
export function queryDebt(partnerId = ''): Promise<QueryPayload> {
  const suffix = partnerId !== '' ? `?partner_id=${encodeURIComponent(partnerId)}` : '';
  return callApi(`/api/v1/query/debt${suffix}`, { method: 'GET' });
}

/** 只读查询：单据 / 流水。 */
export function queryDocs(bizDate = '', docType = ''): Promise<QueryPayload> {
  const params: string[] = [];
  if (bizDate !== '') {
    params.push(`biz_date=${encodeURIComponent(bizDate)}`);
  }
  if (docType !== '') {
    params.push(`doc_type=${encodeURIComponent(docType)}`);
  }
  const suffix = params.length > 0 ? `?${params.join('&')}` : '';
  return callApi(`/api/v1/query/docs${suffix}`, { method: 'GET' });
}

/** 单据摘要（`/query/docs` 的 items 形状）。 */
export interface DocBrief {
  doc_id: string;
  doc_no: string;
  doc_type: string;
  intent_id: string;
  biz_date: string;
  payment_status: string;
  status: string;
  total_amount_fen: number | null;
  total_amount_display: string;
  summary?: string;
  partner_name?: string | null;
  cost_hidden?: boolean;
}

/** 单据明细行（`/query/docs/{id}` 的 lines 形状）。 */
export interface DocLineDetail {
  line_id: string;
  product_id: string;
  product_name?: string;
  qty: string;
  unit: string;
  unit_price_fen: number | null;
  amount_fen: number | null;
  amount_display: string;
}

export interface DocDetail {
  doc: DocBrief;
  lines: DocLineDetail[];
  cost_hidden?: boolean;
  readonly: boolean;
  billed: boolean;
}

/** 最近单据（带 doc_id，改单卡靠它定位）。 */
export function listDocs(limit = 20): Promise<{ items: DocBrief[]; readonly: boolean; billed: boolean }> {
  return callApi(`/api/v1/query/docs?limit=${limit}`, { method: 'GET' });
}

/** 单据详情：摘要 + 明细行（改单卡的「改前」）。 */
export function getDocDetail(docId: string): Promise<DocDetail> {
  return callApi(`/api/v1/query/docs/${docId}`, { method: 'GET' });
}

/** 改单候选：ref 为空或「上一单」时返回最近几条，ambiguous=true 必须让人选。 */
export function listDocCandidates(
  ref = '',
  limit = 3
): Promise<{ items: DocBrief[]; ambiguous: boolean; readonly: boolean }> {
  return callApi(
    `/api/v1/query/docs/candidates?ref=${encodeURIComponent(ref)}&limit=${limit}`,
    { method: 'GET' }
  );
}

/** 商品档案待补与商品级配置。 */
export interface ProductGap {
  product_id: string;
  name: string;
  spec: string;
  base_unit: string;
  last_purchase_price_fen: number | null;
  last_sale_price_fen: number | null;
  avg_cost_fen?: number | null;
  track_batch?: boolean;
  allow_negative?: boolean | null;
  low_stock_qty?: string | null;
  cost_hidden?: boolean;
  status: string;
}

/** 缺进价的商品清单。 */
export function listProductGaps(): Promise<{
  items: ProductGap[];
  readonly: boolean;
  kind: string;
  cost_hidden?: boolean;
}> {
  return callApi('/api/v1/products/gaps', { method: 'GET' });
}

export interface ProductMovementRow {
  doc_id: string;
  doc_no: string;
  doc_type: string;
  biz_date: string;
  qty_base: string;
  unit: string;
  reason?: string;
  unit_cost_fen?: number | null;
  occurred_at?: string;
}

export interface ProductUnitConversionRow {
  from_unit: string;
  to_unit: string;
  ratio: string;
}

export interface ProductDetailPayload {
  product: ProductGap;
  inventory: {
    qty_base: string;
    qty_display: string;
    negative_flag: boolean;
    avg_cost_fen: number | null;
    stock_value_fen: number | null;
  };
  aliases: string[];
  unit_conversions: ProductUnitConversionRow[];
  movements: ProductMovementRow[];
  cost_hidden: boolean;
  readonly: boolean;
}

/** 单个商品详情（档案、门店库存、货值、别名、单位换算与出入库流水）。 */
export function getProductDetail(productId: string): Promise<ProductDetailPayload> {
  return callApi(`/api/v1/products/${productId}`, { method: 'GET' });
}

/** 补商品价格 / 批次开关 / 负库存覆盖。 */
export function patchProductPrice(
  productId: string,
  body: {
    last_purchase_price_fen?: number;
    last_sale_price_fen?: number;
    cost_method?: string;
    track_batch?: boolean;
    allow_negative?: boolean | null;
    low_stock_qty?: number | null;
  }
): Promise<{ product: ProductGap }> {
  return callApi(`/api/v1/products/${productId}`, {
    method: 'PATCH',
    data: body as unknown as Record<string, unknown>,
  });
}

export interface UnifiedGapRow {
  gap_id: string;
  kind: string;
  ref_entity: string;
  ref_id: string;
  title: string;
  detail_json: Record<string, unknown> | null;
  priority: number;
  status: string;
  created_at: string | null;
}

export function listUnifiedGaps(): Promise<{ items: UnifiedGapRow[]; readonly: boolean }> {
  return callApi('/api/v1/products/unified-gaps', { method: 'GET' });
}

export function resolveUnifiedGap(gapId: string): Promise<{ gap_id: string; status: string }> {
  return callApi(`/api/v1/products/unified-gaps/${gapId}/resolve`, { method: 'POST' });
}

/** 设置单位换算（若拆成更细粒度基本单位则原子升级账目）。 */
export function setProductUnitRatio(
  productId: string,
  body: { from_unit: string; to_unit: string; ratio: number }
): Promise<{ unit_ratio: Record<string, unknown> }> {
  return callApi(`/api/v1/products/${productId}/unit-ratio`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
  });
}

export interface MeMemberRow {
  member_id: string;
  name: string;
  role: string;
  status: string;
  is_self: boolean;
}

export interface MePayload {
  member_id: string;
  tenant_id: string;
  store_id: string;
  role: string;
  display_name: string;
  store_name: string;
  plan_code: string;
  accounting_mode: 'per_txn' | 'count_derive';
  allow_negative: boolean;
  staff_can_view_cost: boolean;
  can_view_cost: boolean;
  novice: NoviceStatus;
  auto_post_enabled: boolean;
  alias_count: number;
  members: MeMemberRow[];
}

export function getMe(): Promise<MePayload> {
  return callApi('/api/v1/me', { method: 'GET' });
}

export function updatePreferences(body: {
  auto_post_enabled?: boolean;
  accounting_mode?: 'per_txn' | 'count_derive';
  display_name?: string;
}): Promise<{ display_name: string; accounting_mode: 'per_txn' | 'count_derive'; auto_post_enabled: boolean; novice: NoviceStatus }> {
  return callApi('/api/v1/me/preferences', {
    method: 'PATCH',
    data: body as unknown as Record<string, unknown>,
  });
}

export function deleteDict(dictId: string): Promise<{ dict_id: string; deleted: boolean; dict_version: number }> {
  return callApi(`/api/v1/dict/${dictId}`, { method: 'DELETE' });
}

/** 库存策略：成本法 + 负库存开关 + 店员成本可见性。 */
export interface InventoryPolicy {
  cost_method: 'avg' | 'batch' | 'fifo' | 'fefe' | 'fefo';
  allow_negative: boolean;
  staff_can_view_cost?: boolean;
}

export function getInventoryPolicy(): Promise<InventoryPolicy> {
  return callApi('/api/v1/inventory/policy', { method: 'GET' });
}

export function patchInventoryPolicy(body: {
  cost_method?: 'avg' | 'batch' | 'fifo' | 'fefe' | 'fefo';
  allow_negative?: boolean;
  staff_can_view_cost?: boolean;
}): Promise<InventoryPolicy> {
  return callApi('/api/v1/inventory/policy', {
    method: 'PATCH',
    data: body as unknown as Record<string, unknown>,
  });
}

/** 批次余量。 */
export interface StockBatchRow {
  batch_id: string;
  product_id: string;
  product_name?: string;
  base_unit?: string;
  label?: string;
  is_virtual?: boolean;
  qty_in: string;
  qty_left: string;
  unit_cost_fen: number | null;
  cost_hidden?: boolean;
  expiry_date: string | null;
  status: string;
  received_at: string;
}

export function listBatches(
  productId = ''
): Promise<{ items: StockBatchRow[]; cost_hidden?: boolean; readonly: boolean }> {
  const query = productId === '' ? '' : `?product_id=${productId}`;
  return callApi(`/api/v1/inventory/batches${query}`, { method: 'GET' });
}

/** 盘点。 */
export interface CountLineRow {
  product_id: string;
  name: string;
  unit: string;
  qty_book: string;
  qty_counted: string | null;
  diff: string | null;
  reason: string | null;
}

export interface CountDetail {
  count: { count_id: string; status: string; started_at: string; applied_at: string | null };
  lines: CountLineRow[];
}

export function startCount(): Promise<{ count_id: string; status: string }> {
  return callApi('/api/v1/counts', { method: 'POST' });
}

export function getCount(countId: string): Promise<CountDetail> {
  return callApi(`/api/v1/counts/${countId}`, { method: 'GET' });
}

export function fillCountLine(
  countId: string,
  productId: string,
  qtyCounted: string,
  reason?: string
): Promise<{ product_id: string; qty_book: string; qty_counted: string }> {
  return callApi(`/api/v1/counts/${countId}/lines/${productId}`, {
    method: 'PATCH',
    data: { qty_counted: qtyCounted, reason: reason ?? null } as unknown as Record<string, unknown>,
  });
}

export function applyCount(
  countId: string
): Promise<{
  result: {
    count_id: string;
    adjusted_lines: number;
    total_qty_diff: string;
    doc_id?: string | null;
    accounting_mode?: string;
    derived_sales_doc_id?: string | null;
    derived_sales_amount_fen?: number;
  };
}> {
  return callApi(`/api/v1/counts/${countId}/apply`, { method: 'POST' });
}

/** 撤销一张单（反做单，原单保留）。 */
export function reverseDoc(
  docId: string,
  reason = ''
): Promise<{ result: { reversal_doc_id?: string; reversal_doc_no?: string; detail?: string }; window_minutes: number }> {
  return callApi(`/api/v1/docs/${docId}/reversal`, {
    method: 'POST',
    data: { reason } as unknown as Record<string, unknown>,
    idempotencyKey: `reversal:${docId}`,
  });
}

/** 一步原子改单（同一事务反做原单 + 新建更正单）。 */
export function correctDoc(
  docId: string,
  body: {
    lines: Array<{
      product_id: string;
      qty: number;
      unit?: string;
      unit_price_fen?: number | null;
    }>;
    reason?: string;
  }
): Promise<{
  result: {
    original_doc_id: string;
    reversal_doc_id: string;
    new_doc_id: string;
    new_doc_no: string;
    total_amount_fen: number;
    detail: string;
  };
}> {
  return callApi(`/api/v1/docs/${docId}/correct`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyScope: `correct:${docId}`,
  });
}

/** 只读查询：报表（`kind=top` 时返回 `top_products`，其余返回 `report`，支持 today/week/month）。 */
export function queryReport(
  kind: 'today' | 'week' | 'month' | 'profit' | 'top',
  period: 'today' | 'week' | 'month' = 'today'
): Promise<QueryPayload> {
  if (kind === 'top') {
    return callApi('/api/v1/query/report?top=true', { method: 'GET' });
  }
  const resolvedPeriod = kind === 'week' || kind === 'month' ? kind : period;
  return callApi(`/api/v1/query/report?period=${resolvedPeriod}`, { method: 'GET' });
}

/** 用量（**自然月**口径，与权益的 30 天滚动窗口分开）。 */
export function usage(): Promise<UsageEnvelope> {
  return callApi<UsageEnvelope>('/api/v1/usage', { method: 'GET' });
}

/** 权益（**30 天滚动窗口**口径）+ 用量，两份口径并列返回。 */
export function entitlement(): Promise<EntitlementEnvelope> {
  return callApi<EntitlementEnvelope>('/api/v1/entitlement', { method: 'GET' });
}

/** 道具清单（价格来自 `goods.json`，端上不硬编码价格）。 */
export function listGoods(): Promise<GoodsPayload> {
  return callApi<GoodsPayload>('/api/v1/entitlement/goods', { method: 'GET' });
}

/** 下单（**前端 success 不发货**，发货只认微信支付推送）。 */
export function createOrder(goodsId: string): Promise<OrderPayload> {
  return callApi<OrderPayload>('/api/v1/entitlement/order', {
    method: 'POST',
    data: { goods_id: goodsId },
    idempotencyScope: `order:${goodsId}`,
  });
}

/** R3 待办（**仅电脑版微信大屏 + role=boss**）。 */
export function listTasks(): Promise<{
  items: PendingTaskRow[];
  hint_zh: string;
  note: string;
}> {
  return callApi('/api/v1/tasks', { method: 'GET' });
}

/**
 * 审批 R3 待办（**必须二次确认**；手机端会拿到 `E_R3_PC_REQUIRED` 1206）。
 *
 * 入参字段与服务端 `ApproveRequest` 严格对齐：``approve`` / ``second_confirm`` / ``device``。
 */
export function approveTask(
  taskId: string,
  body: { approve: boolean; second_confirm: boolean; device?: string }
): Promise<{ decision: TaskDecisionPayload; hint_zh: string; idempotency_key: string }> {
  return callApi(`/api/v1/tasks/${taskId}/approve`, {
    method: 'POST',
    data: { device: 'pc_bigscreen', ...body } as unknown as Record<string, unknown>,
    idempotencyKey: `task:${taskId}`,
  });
}

/** 导出任务列表（含交付方式说明：**不提供网页下载**）。 */
export function listExports(): Promise<{ items: ExportJobRow[]; delivery: string }> {
  return callApi('/api/v1/exports', { method: 'GET' });
}

/** 创建导出（R3，异步；只在电脑版微信大屏发起）。 */
export function createExport(
  scope: 'docs' | 'stock' | 'debt' | 'audit' | 'usage',
  fileFormat: 'csv' | 'xlsx' = 'csv'
): Promise<{ export: ExportJobRow; allowed_scopes: string[]; note: string }> {
  return callApi('/api/v1/exports', {
    method: 'POST',
    data: { scope, file_format: fileFormat },
    idempotencyScope: `export:${scope}:${fileFormat}`,
  });
}

/** 个人词表 / 模板候选（说法训练审核）。 */
export function listDict(): Promise<{ items: DictRow[] }> {
  return callApi('/api/v1/dict', { method: 'GET' });
}

/**
 * 教一句话（写入个人词表，`dict_version` 递增击穿缓存）。
 *
 * 入参与服务端 `TeachRequest` 对齐：``raw_text`` / ``mapped_type`` / ``mapped_id``。
 */
export function teachDict(body: {
  raw_text: string;
  mapped_type?: string;
  mapped_id?: string;
  target_name?: string;
}): Promise<{
  dict_id: string;
  raw_text: string;
  mapped_type: string;
  mapped_type_label?: string;
  mapped_id?: string | null;
  mapped_name?: string | null;
  dict_version: number;
  effective_immediately: boolean;
  note: string;
}> {
  return callApi('/api/v1/dict/teach', {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyScope: `teach:${body.raw_text}`,
  });
}

export interface DeliveryNoteLine {
  line_index?: number;
  product_name: string;
  unit: string;
  quantity: number;
  unit_price_fen: number | null;
  amount_fen: number | null;
  matched_product_id?: string | null;
  matched_product_name?: string | null;
  matched_base_unit?: string | null;
  is_new_product?: boolean;
  needs_unit_ratio?: boolean;
  unit_ratio?: string | null;
}

export interface DeliveryNoteDraft {
  attachment_id: string;
  object_ref: string;
  supplier_name: string | null;
  doc_date: string;
  payment_status: '已结' | '未结' | string;
  stated_total_fen: number | null;
  computed_total_fen: number | null;
  total_mismatch: boolean;
  total_diff_fen: number | null;
  warnings: string[];
  lines: DeliveryNoteLine[];
  notes: string;
  model: string;
  cost_hidden?: boolean;
}

/** 拍进货单识别（调用中国开源视觉模型提取明细，不直接写账）。 */
export function parseDeliveryNote(body: {
  image_base64?: string;
  image_url?: string;
  object_ref?: string;
  hint_text?: string;
}): Promise<DeliveryNoteDraft> {
  return callApi<DeliveryNoteDraft>('/api/v1/delivery-note/parse', {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyScope: `dn-parse:${body.object_ref ?? Date.now()}`,
    timeoutMs: 30000,
  });
}

/** 核对进货单并确认入库（通过 LedgerService 记账并标注 source=photo）。 */
export function confirmDeliveryNote(
  attachmentId: string,
  body: {
    supplier_name?: string | null;
    doc_date?: string | null;
    payment_status?: string;
    stated_total_fen?: number | null;
    lines: Array<{
      product_name: string;
      unit: string;
      quantity: number;
      unit_price_fen?: number | null;
      amount_fen?: number | null;
      product_id?: string | null;
      matched_product_id?: string | null;
      unit_ratio?: string | number | null;
    }>;
  }
): Promise<{
  attachment_id: string;
  doc_id: string;
  doc_no: string;
  total_amount_fen: number;
  payment_status: string;
  source: string;
  line_count: number;
}> {
  return callApi(`/api/v1/delivery-note/${attachmentId}/confirm`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyScope: `dn-confirm:${attachmentId}`,
  });
}

/** 查询最近拍过的进货单与关联单据状态。 */
export function listDeliveryNotes(limit = 20): Promise<{
  items: Array<{
    attachment_id: string;
    doc_id: string | null;
    object_ref: string;
    supplier_name: string | null;
    computed_total_fen: number | null;
    line_count: number;
    created_at: string | null;
  }>;
}> {
  return callApi(`/api/v1/delivery-note/attachments?limit=${limit}`, { method: 'GET' });
}

/** 获取导出任务的 UTF-8 BOM CSV 内容，供本地写入与微信转发。 */
export function getExportContent(jobId: string): Promise<{
  job_id: string;
  scope: string;
  filename: string;
  row_count: number;
  mime: string;
  csv_content: string;
}> {
  return callApi(`/api/v1/exports/${jobId}/content`, { method: 'GET' });
}

/** 合并重复商品。 */
export function mergeProduct(
  sourceProductId: string,
  targetProductId: string
): Promise<{
  merge: {
    source_product_id: string;
    target_product_id: string;
    target_name: string;
    target_base_unit: string;
    unit_factor: string;
    moved_inventory_rows: number;
    moved_doc_lines: number;
    moved_movements: number;
    moved_batches: number;
    moved_aliases: number;
  };
}> {
  return callApi(`/api/v1/products/${sourceProductId}/merge`, {
    method: 'POST',
    data: { target_product_id: targetProductId },
    idempotencyScope: `merge:${sourceProductId}:${targetProductId}`,
  });
}

/** 欠款页直接登记收款/付款核销。 */
export function settleDebt(body: {
  partner_id: string;
  direction: 'receivable' | 'payable';
  amount_fen: number;
  method?: string;
}): Promise<{
  doc_id: string;
  doc_no: string;
  partner_id: string;
  partner_name: string;
  direction: string;
  settled_fen: number;
  remaining_outstanding_fen: number;
  method: string;
}> {
  return callApi('/api/v1/debts/settle', {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyScope: `debt-settle:${body.partner_id}:${Date.now()}`,
  });
}

/** 获取客户/供应商对账单数据。 */
export function getPartnerStatement(
  partnerId: string,
  direction?: 'receivable' | 'payable'
): Promise<{
  store_name: string;
  partner_id: string;
  partner_name: string;
  direction: string;
  generated_at: string;
  total_outstanding_fen: number;
  items: Array<{
    ar_ap_id: string;
    doc_id: string;
    doc_no: string;
    biz_date: string;
    direction: string;
    amount_fen: number;
    settled_amount_fen: number;
    outstanding_fen: number;
    status: string;
  }>;
}> {
  const q = direction ? `?direction=${direction}` : '';
  return callApi(`/api/v1/debts/${partnerId}/statement${q}`, { method: 'GET' });
}

/** 查询当前门店未结束的盘点单。 */
export function getOpenCount(): Promise<{
  count: { count_id: string; status: string; started_at: string } | null;
}> {
  return callApi('/api/v1/counts/open', { method: 'GET' });
}

/** 语音连续报数批量填盘点。 */
export function voiceBatchCount(
  countId: string,
  spokenText: string
): Promise<{
  matched: Array<{ product_id: string; name: string; qty_counted: string; unit: string }>;
  unmatched: string[];
}> {
  return callApi(`/api/v1/counts/${countId}/voice-batch`, {
    method: 'POST',
    data: { spoken_text: spokenText },
    idempotencyScope: `count-voice:${countId}:${Date.now()}`,
  });
}

/** 老板生成店员邀请码。 */
export function createStaffInvite(roleCode: 'clerk' | 'manager' | 'readonly' = 'clerk'): Promise<{
  invite_code: string;
  role_code: string;
  expires_at: string;
  ttl_hours: number;
}> {
  return callApi('/api/v1/me/staff/invite', {
    method: 'POST',
    data: { role_code: roleCode, ttl_hours: 24 },
  });
}

/** 凭邀请码加入店铺。 */
export function joinStaffByInvite(
  inviteCode: string,
  displayName?: string
): Promise<{
  jwt: string;
  tenant_id: string;
  store_id: string;
  member_id: string;
  role: string;
  store_name: string;
  member_name: string;
}> {
  return callApi('/api/v1/me/staff/join', {
    method: 'POST',
    data: { invite_code: inviteCode, display_name: displayName ?? null },
  });
}

/** 老板启用/停用店员。 */
export function updateStaffStatus(
  memberId: string,
  body: { status?: 'active' | 'disabled'; role_code?: 'boss' | 'manager' | 'clerk' | 'readonly' }
): Promise<{ member_id: string; name: string; status: string; role: string }> {
  return callApi(`/api/v1/me/staff/${memberId}`, {
    method: 'PATCH',
    data: body as unknown as Record<string, unknown>,
  });
}

/** 生成或发送今日经营日报（三账体检 + 订阅消息推送）。 */
export function sendClosingSummary(): Promise<{
  sent: boolean;
  dry_run: boolean;
  reason?: string;
  summary: {
    biz_date: string;
    doc_count: number;
    sales_fen: number;
    purchase_fen: number;
    gross_profit_fen: number | null;
    reconcile_ok: boolean;
    negative_count: number;
    summary_text: string;
  };
}> {
  return callApi('/api/v1/me/closing-summary/send', { method: 'POST', data: {} });
}
