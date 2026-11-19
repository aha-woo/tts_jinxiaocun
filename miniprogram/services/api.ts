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
import { nextIdempotencyKey } from '../utils/dedupe';

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

const DEFAULT_TIMEOUT_MS = 8000;
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
}

/** 澄清追问。 */
export interface ClarifyPayload {
  slot: string;
  question: string;
  options: string[];
}

/** 创建会话的返回（含上传票据；**音频不进请求体**）。 */
export interface SessionPayload {
  session_id: string;
  state: string;
  audio_object_ref: string;
  upload_ticket: {
    ticket_id: string;
    audio_object_ref: string;
    upload_prefix: string;
    expires_at: string;
    max_bytes: number;
  };
  quota: { remaining_minute: number; scope: string };
  degrade_level: string;
  note: string;
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
  answer?: { kind: string; items: unknown[]; note?: string } | null;
  /** route=card */
  card?: CardPayload | null;
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
  quota: number;
  cost_fen: number;
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
  valid_until: string;
  days_left: number;
  remaining_quota: number;
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
  name: string;
  price_fen: number;
  price_text: string;
  desc: string;
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
  mapped_id: string | null;
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
export async function callApi<T>(path: string, options: RequestOptions = {}): Promise<T> {
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
  body: { audio_object_ref?: string; asr_text?: string; asr_confidence?: number; duration_ms?: number }
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
  body: { confirm_type: 'button' | 'voice2'; button_fallback?: boolean; spoken_text?: string }
): Promise<ConfirmEnvelope> {
  return callApi<ConfirmEnvelope>(`/api/v1/voice/cards/${cardId}/confirm`, {
    method: 'POST',
    data: body as unknown as Record<string, unknown>,
    idempotencyKey: `confirm:${cardId}`,
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

/** 编辑卡片某个槽位（**本地结构化重算，不重调 AI**）。 */
export function editCard(
  cardId: string,
  body: { code: string; value: string | number }
): Promise<CardPayload & { llm_calls: number; note: string }> {
  return callApi(`/api/v1/voice/cards/${cardId}/edit`, {
    method: 'PATCH',
    data: body as unknown as Record<string, unknown>,
  });
}

/** 待确认卡片列表。 */
export function listPending(): Promise<{ items: CardPayload[]; note: string }> {
  return callApi('/api/v1/voice/cards/pending', { method: 'GET' });
}

/** 只读查询：库存（R0，不建卡、不扣次）。 */
export function queryStock(keyword = ''): Promise<QueryPayload> {
  return callApi(`/api/v1/query/stock?keyword=${encodeURIComponent(keyword)}`, { method: 'GET' });
}

/** 只读查询：欠款。 */
export function queryDebt(keyword = ''): Promise<QueryPayload> {
  return callApi(`/api/v1/query/debt?keyword=${encodeURIComponent(keyword)}`, { method: 'GET' });
}

/** 只读查询：单据 / 流水。 */
export function queryDocs(keyword = ''): Promise<QueryPayload> {
  return callApi(`/api/v1/query/docs?keyword=${encodeURIComponent(keyword)}`, { method: 'GET' });
}

/** 只读查询：报表（`kind=top` 时返回 `top_products`，其余返回 `report`）。 */
export function queryReport(kind: 'today' | 'month' | 'profit' | 'top'): Promise<QueryPayload> {
  return callApi(`/api/v1/query/report?kind=${kind}`, { method: 'GET' });
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
): Promise<TaskDecisionPayload & { hint_zh: string; idempotency_key: string }> {
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
    idempotencyKey: `export:${scope}:${fileFormat}`,
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
}): Promise<{
  dict_id: string;
  raw_text: string;
  mapped_type: string;
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
