/**
 * 云函数：xpay_goods_deliver_notify（小程序虚拟支付发货推送 · 主路径）
 *
 * 边界（docs/02-架构设计.md D13 / §5.4）
 * -------------------------------------
 * - 发货**只以发货推送为准**：小程序端 `wx.requestVirtualPayment` 的 success 不作发货依据；
 * - 幂等键 = `outTradeNo`，落 `deliver_event` 时依赖 `UNIQUE(tenant_id, out_trade_no)`，
 *   重复推送返回 `E_DELIVER_DUPLICATE`，**不重复发额度**；
 * - 验签失败返回 `E_PAY_SIGN_INVALID`，不落任何权益；
 * - 发货动作 = `usage_quota` 加额度 / 延长 `entitlement.valid_until`，**不写 `usage_ledger`**
 *   （权益发放与语音扣次分离，A10）；
 * - 本文件 T01 仅交付**骨架**：验签、幂等落库、调服务端发放三步都留了明确的 TODO，
 *   真正实现在 T03（`server/app/pay/deliver_callback.py`）+ T04（`entitlement.py`）。
 *
 * 环境变量（云开发控制台配置，禁止写死在代码里）
 * - `XPAY_APP_KEY`：现网 AppKey（**沙箱 Key 不得用于生产**）
 * - `DELIVER_CALLBACK_URL`：云托管 `/entitlement/deliver-callback` 内网地址
 * - `DELIVER_CALLBACK_TOKEN`：服务端间调用令牌
 */

'use strict';

const cloud = require('wx-server-sdk');

cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });

/** 错误码常量：与 packages/shared/contracts/error-codes.json 保持一致。 */
const ERROR_CODES = {
  PAY_SIGN_INVALID: 'E_PAY_SIGN_INVALID',
  DELIVER_DUPLICATE: 'E_DELIVER_DUPLICATE',
  VALIDATION: 'E_VALIDATION',
  INTERNAL: 'E_INTERNAL',
};

const DELIVER_COLLECTION = 'deliver_event';

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
 * 幂等占位：先在 deliver_event 里抢占 outTradeNo。
 *
 * TODO(T02/T03): 改为服务端 `INSERT ... ON CONFLICT DO NOTHING` 或云开发数据库唯一索引，
 * 保证并发推送下也只发一次额度；当前实现仅用于骨架联调。
 *
 * @param {string} tenantId 租户
 * @param {string} outTradeNo 订单号（幂等键）
 * @returns {Promise<boolean>} true=首次处理；false=重复推送
 */
async function claimDeliverEvent(tenantId, outTradeNo) {
  const db = cloud.database();
  const existing = await db
    .collection(DELIVER_COLLECTION)
    .where({ tenant_id: tenantId, out_trade_no: outTradeNo })
    .limit(1)
    .get();
  if (existing.data && existing.data.length > 0) {
    return false;
  }
  await db.collection(DELIVER_COLLECTION).add({
    data: {
      tenant_id: tenantId,
      out_trade_no: outTradeNo,
      source: 'xpay_goods_deliver_notify',
      sign_status: 'verified',
      delivered: false,
      received_at: new Date().toISOString(),
    },
  });
  return true;
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

  if (!outTradeNo || !tenantId) {
    return {
      code: 1502,
      data: { error_code: ERROR_CODES.VALIDATION },
      message: '发货推送缺少 outTradeNo / tenant_id',
    };
  }

  if (!verifySign(event)) {
    console.warn('[deliver] 验签失败', { outTradeNo });
    return {
      code: 1401,
      data: { error_code: ERROR_CODES.PAY_SIGN_INVALID },
      message: '验签失败，未发货',
    };
  }

  try {
    const firstTime = await claimDeliverEvent(tenantId, outTradeNo);
    if (!firstTime) {
      return {
        code: 1402,
        data: { error_code: ERROR_CODES.DELIVER_DUPLICATE, out_trade_no: outTradeNo },
        message: '重复推送，已幂等忽略（不重复发额度）',
      };
    }

    // TODO(T04): 调用云托管 /entitlement/deliver-callback（内网 + 服务间令牌），
    // 由服务端完成：加 usage_quota 额度 / 延长 entitlement.valid_until，
    // 并把 deliver_event.delivered 置 true。**发货不写 usage_ledger**。
    const callbackUrl = process.env.DELIVER_CALLBACK_URL;
    if (!callbackUrl) {
      console.warn('[deliver] 未配置 DELIVER_CALLBACK_URL，仅记录事件', { outTradeNo });
      return {
        code: 0,
        data: { out_trade_no: outTradeNo, delivered: false, replay_safe: true },
        message: '已记录发货事件（权益发放待 T04 接通）',
      };
    }

    console.log('[deliver] 待接通服务端发放', { outTradeNo, callbackUrl });
    return {
      code: 0,
      data: { out_trade_no: outTradeNo, delivered: false },
      message: '骨架：待接通服务端权益发放（T04）',
    };
  } catch (error) {
    console.error('[deliver] 处理异常', error);
    return {
      code: 1503,
      data: { error_code: ERROR_CODES.INTERNAL },
      message: '发货回调处理异常，等待平台重推或 query_order 兜底',
    };
  }
};
