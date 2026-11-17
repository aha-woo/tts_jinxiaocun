/**
 * 云函数：xpay_goods_deliver_notify（小程序虚拟支付发货推送 · 主路径）
 *
 * 边界（docs/02-架构设计.md D13 / §5.4）
 * -------------------------------------
 * - 发货**只以发货推送为准**：小程序端 `wx.requestVirtualPayment` 的 success 不作发货依据；
 * - 幂等键 = `outTradeNo`，落 `deliver_event` 时依赖 `UNIQUE(tenant_id, out_trade_no)`，
 *   重复推送返回 `E_DELIVER_DUPLICATE`(1402)，**不重复发额度**；
 * - 验签失败返回 `E_PAY_SIGN_INVALID`，不落任何权益；
 * - 发货动作 = `usage_quota` 加额度 / 延长 `entitlement.valid_until`，**不写 `usage_ledger`**
 *   （权益发放与语音扣次分离，A10）；
 * - 真正的发放逻辑在服务端（`server/app/api/v1/entitlement.py` 的
 *   `POST /entitlement/deliver-callback`）。本函数只做「验签 + 转发 + 如实回报」。
 *
 * **不吞异常原则**（P0-3）
 * ------------------------
 * 付了钱没拿到额度，且云函数返回「成功」= 最坏的故障形态：用户投诉无门、日志无痕、
 * 平台也不会重推。因此本函数只在**服务端明确回执已发货**时返回成功码；
 * 任何其它情况（未配置、令牌错、网络断、服务端 5xx、服务端说「没发货」）都返回
 * 非 0 业务码 + 错误详情，并打 `OutTradeNo`，让微信重推 / 人工介入。
 * 「重复推送」是**幂等成功**（1402），直接返回给平台，不算失败、也不必重推。
 *
 * 环境变量（云开发控制台配置，禁止写死在代码里）
 * --------------------------------------------
 * - `XPAY_APP_KEY`：现网 AppKey（**沙箱 Key 不得用于生产**）
 * - `DELIVER_CALLBACK_URL`：云托管 `/api/v1/entitlement/deliver-callback` 内网地址
 * - `SERVICE_TOKEN`：服务间令牌，**必须与云托管同名环境变量一致**
 *   （服务端读取同名变量并用 `hmac.compare_digest` 比对）
 */

'use strict';

const cloud = require('wx-server-sdk');

cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });

/** 业务码：与 packages/shared/contracts/error-codes.json 的 numeric **一一对齐**。 */
const CODES = {
  OK: 0,
  PAY_SIGN_INVALID: 1401,
  DELIVER_DUPLICATE: 1402,
  VALIDATION: 1502,
  INTERNAL: 1503,
};
// 注意：不要自造 `1501`。契约里 1501 = `E_DUPLICATE_REQUEST`（http 409），
// 若拿它表示「发货未接通」，同一个数字就有了两个含义，排查时必然误导。
// 「云函数没配好」属于运维配置错误 → 用现成的 `E_INTERNAL`(1503)，别新增错误码（总数必须仍是 25）。

/** 字符串错误码：与 contracts/error-codes.json 一致（**全部已登记**）。 */
const ERROR_CODES = {
  PAY_SIGN_INVALID: 'E_PAY_SIGN_INVALID',
  DELIVER_DUPLICATE: 'E_DELIVER_DUPLICATE',
  VALIDATION: 'E_VALIDATION',
  INTERNAL: 'E_INTERNAL',
  CALLBACK_FAILED: 'E_INTERNAL',
};

/** 默认发货回调超时（毫秒）：**必须设**，否则云函数会被挂到平台超时 */
const CALLBACK_TIMEOUT_MS = 8000;

/**
 * 校验发货推送签名。
 *
 * TODO(T03): 按官方虚拟支付《发货推送》文档实现真实验签（字段顺序、签名算法、时间戳容差），
 * 并补单测覆盖「签名错 / 时间戳过期 / 报文被篡改」三类用例。
 *
 * @param {Record<string, unknown>} event 推送报文
 * @returns {boolean} 是否验签通过
 */
function verifySign(event) {
  const appKey = process.env.XPAY_APP_KEY;
  if (!appKey) {
    // 未配置密钥时**拒绝发货**（宁可不发，也不误发额度）
    console.error('[deliver] 缺少 XPAY_APP_KEY，拒绝发货');
    return false;
  }
  const signature = event && (event.PaySig || event.signature);
  if (typeof signature !== 'string' || signature.length === 0) {
    return false;
  }
  // TODO(T03): 真实验签实现占位——当前仅做「有签名 + 有密钥」的保守判定
  return true;
}

/**
 * 调服务端完成真正的额度/权益发放。
 *
 * 返回值的语义（调用方据此决定是否让微信重推）：
 * - `{ settled: true }`：已发货；
 * - `{ settled: true, duplicate: true }`：重复推送，幂等成功；
 * - `{ settled: false, code, errorCode }`：**未发货**，必须让上游重推/告警。
 *
 * 为什么不在这里抢 `deliver_event`（原先的 `claimDeliverEvent` 已删除）
 * ------------------------------------------------------------------
 * 幂等只有**一个**权威：服务端的 `UNIQUE(tenant_id, out_trade_no)` + `E_DELIVER_DUPLICATE`。
 * 云函数先在本地集合里插一条 `delivered:false` 的行，一旦后续服务端调用失败，
 * 微信重推时就会被自己这条「占位」判成重复 → 永远不再发货，而用户已经付钱。
 * 两层幂等互相打架 = 静默丢单，所以这一层去掉。
 *
 * @param {Record<string, unknown>} event 原始推送报文
 * @param {string} outTradeNo 订单号
 * @param {string} tenantId 租户
 * @returns {Promise<{settled: boolean, duplicate?: boolean, code?: number, errorCode?: string, detail?: unknown}>}
 */
async function callServerDeliver(event, outTradeNo, tenantId) {
  const callbackUrl = process.env.DELIVER_CALLBACK_URL;
  const serviceToken = process.env.SERVICE_TOKEN;

  if (!callbackUrl || !serviceToken) {
    // 不许把「没接通」伪装成「处理完毕」
    console.error(
      '[deliver] 未配置 DELIVER_CALLBACK_URL / SERVICE_TOKEN，发货未接通',
      { outTradeNo }
    );
    return { settled: false, code: CODES.INTERNAL, errorCode: ERROR_CODES.INTERNAL };
  }

  if (typeof fetch !== 'function') {
    // 运行时低于 Node 18 时没有全局 fetch：显式报错，不要静默变成「发不出去的成功」
    console.error(
      '[deliver] 当前运行时没有全局 fetch（需 Node 18+ 或显式引入 node-fetch），发货未接通',
      { outTradeNo }
    );
    return { settled: false, code: CODES.INTERNAL, errorCode: ERROR_CODES.INTERNAL };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CALLBACK_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(callbackUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // 服务端只认这两个头：通道白名单 + 服务间令牌（常量时间比较）
        'X-Wx-Channel': 'cloudfunc',
        'X-Service-Token': serviceToken,
        'X-Tenant-Id': tenantId,
      },
      body: JSON.stringify(event),
      signal: controller.signal,
    });
  } catch (error) {
    // 网络层失败（DNS/超时/连接重置）：**不吞**，如实回报「未发货」让平台重推
    console.error('[deliver] 调用服务端发放失败（网络层）', { outTradeNo, error: String(error) });
    return { settled: false, code: CODES.INTERNAL, errorCode: ERROR_CODES.CALLBACK_FAILED };
  } finally {
    clearTimeout(timer);
  }

  const body = await response.json().catch(() => ({}));
  const data = (body && body.data) || {};
  console.log('[deliver] 服务端发放回执', {
    outTradeNo,
    httpStatus: response.status,
    envelopeCode: body && body.code,
    errorCode: data.error_code,
    delivered: data.delivered,
    grantQuota: data.grant_quota,
  });

  // 幂等重复：服务端回 1402（HTTP 200），对平台而言是「已处理」，不必重推
  if (
    data.error_code === ERROR_CODES.DELIVER_DUPLICATE ||
    (body && body.code === CODES.DELIVER_DUPLICATE)
  ) {
    return { settled: true, duplicate: true };
  }

  if (!response.ok) {
    // 401/403/404/422/503 一律视为**未发货**：把服务端错误码原样带出去，便于定位
    return {
      settled: false,
      code: (body && body.code) || CODES.INTERNAL,
      errorCode: data.error_code || ERROR_CODES.CALLBACK_FAILED,
      detail: body && body.message,
    };
  }

  if (!data.delivered) {
    // HTTP 200 但服务端说「没发货」：绝不能当成功
    return {
      settled: false,
      code: CODES.INTERNAL,
      errorCode: data.error_code || ERROR_CODES.CALLBACK_FAILED,
      detail: data.reason,
    };
  }

  return { settled: true };
}

/**
 * 云函数入口。
 *
 * @param {Record<string, unknown>} event 推送报文
 * @returns {Promise<{code: number, data: unknown, message: string}>} 统一包络
 */
exports.main = async (event) => {
  const outTradeNo = event && (event.OutTradeNo || event.out_trade_no);
  const tenantId = event && (event.TenantId || event.tenant_id);

  console.log('[deliver] 收到发货推送', { outTradeNo, tenantId });

  if (!outTradeNo || !tenantId) {
    console.error('[deliver] 推送缺少 outTradeNo / tenant_id', { outTradeNo, tenantId });
    return {
      code: CODES.VALIDATION,
      data: { error_code: ERROR_CODES.VALIDATION },
      message: '发货推送缺少 outTradeNo / tenant_id',
    };
  }

  if (!verifySign(event)) {
    console.warn('[deliver] 验签失败', { outTradeNo });
    return {
      code: CODES.PAY_SIGN_INVALID,
      data: { error_code: ERROR_CODES.PAY_SIGN_INVALID, out_trade_no: outTradeNo },
      message: '验签失败，未发货',
    };
  }

  try {
    const outcome = await callServerDeliver(event, outTradeNo, tenantId);

    if (!outcome.settled) {
      console.error('[deliver] 服务端未完成发放，本条**未发货**（等待平台重推 / query_order 兜底）', {
        outTradeNo,
        errorCode: outcome.errorCode,
        detail: outcome.detail,
      });
      return {
        code: outcome.code || CODES.INTERNAL,
        data: {
          out_trade_no: outTradeNo,
          delivered: false,
          error_code: outcome.errorCode,
        },
        message: '服务端未完成发放，本条未发货（详见云托管日志）',
      };
    }

    if (outcome.duplicate) {
      return {
        code: CODES.DELIVER_DUPLICATE,
        data: { out_trade_no: outTradeNo, delivered: true, error_code: ERROR_CODES.DELIVER_DUPLICATE },
        message: '服务端判定为重复推送，已幂等忽略（额度未重复发放）',
      };
    }

    return {
      code: CODES.OK,
      data: { out_trade_no: outTradeNo, delivered: true },
      message: '已发货',
    };
  } catch (error) {
    // 走到这里说明发生了未预期的异常：**照样如实回报失败**，不伪装成功
    console.error('[deliver] 处理异常', { outTradeNo, error: String(error) });
    return {
      code: CODES.INTERNAL,
      data: { out_trade_no: outTradeNo, delivered: false, error_code: ERROR_CODES.INTERNAL },
      message: '发货回调处理异常，本条未确认发货，等待平台重推或 query_order 兜底',
    };
  }
};
