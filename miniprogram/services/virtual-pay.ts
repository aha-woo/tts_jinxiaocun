/**
 * 虚拟支付：**付费入口只在小程序内**（电脑版微信也可购买），无独立网页版、无账号密码。
 *
 * 一条铁律（架构 §8.4「发货只以推送为准」）
 * ---------------------------------------
 * 前端 `success` 回调**只代表「用户付完了」**，**不代表额度到账**：
 * 真正的发货由微信支付的服务端推送触发（云开发云函数 `xpay_deliver_notify`
 * → 验签 → `outTradeNo` 幂等 → 加额度/延期）。
 *
 * 所以本模块的 `success` 分支只做一件事：**提示「正在到账」并轮询权益**，
 * 绝不本地改额度（改了就会「没付钱也能用」）。
 *
 * ⚠️ 平台能力标注：
 * - 微信虚拟支付 API（`wx.requestVirtualPayment`）在 Windows 电脑版微信的可用性
 *   **需核实**；不可用时按「引导到手机端购买」降级，不做任何 H5 兜底；
 * - 费率 / 最低金额 / iOS 分成 **均需核实**，端上不写死任何金额（价格一律来自服务端
 *   下发的 `goods.json`）。
 */

import { createOrder, entitlement, type EntitlementPayload } from './api';

/** 下单 + 拉起支付的入参。 */
export interface PurchaseResult {
  /** 商户单号（发货幂等的唯一键） */
  outTradeNo: string;
  /** `success` 只代表支付完成；**额度是否到账要看 {@link pollEntitlement}** */
  paid: boolean;
  message: string;
}

type VirtualPaymentApi = {
  requestVirtualPayment?: (params: Record<string, unknown>) => void;
};

/** 当前环境是否支持小程序内虚拟支付（不支持时引导到手机端，**不做 H5**）。 */
export function canPurchaseHere(): boolean {
  const api = wx as unknown as VirtualPaymentApi;
  return typeof api.requestVirtualPayment === 'function';
}

/**
 * 发起一次购买。
 *
 * @param goodsId 道具 id（价格由服务端按 `goods.json` 决定，端上不传价）
 * @returns 商户单号 + 是否已支付（不代表到账）
 */
export async function purchase(goodsId: string): Promise<PurchaseResult> {
  if (!canPurchaseHere()) {
    return {
      outTradeNo: '',
      paid: false,
      message: '当前版本微信不支持小程序内购买，请在手机端微信里完成购买（没有网页版入口）',
    };
  }
  // 真实结构是 `{order: {...}, deliver_rule}`（见 app/api/v1/entitlement.py）
  const envelope = await createOrder(goodsId);
  const order = envelope.order;
  const api = wx as unknown as VirtualPaymentApi;
  const request = api.requestVirtualPayment;
  if (request === undefined) {
    return { outTradeNo: order.out_trade_no, paid: false, message: '当前微信不支持虚拟支付' };
  }

  const paid = await new Promise<boolean>((resolve) => {
    request({
      ...order.pay_params,
      success: () => resolve(true),
      fail: () => resolve(false),
    });
  });

  return {
    outTradeNo: order.out_trade_no,
    paid,
    message: paid ? '支付完成，额度由微信服务端推送后到账' : '未完成支付',
  };
}

/**
 * 轮询权益直到额度到账（**发货只认服务端推送**，端上只读不写）。
 *
 * @param beforeRemaining 支付前的剩余次数（用来判断有没有真的加上）
 * @param attempts 最多轮询几次（默认 6 次，每次 1.5s，覆盖推送延迟）
 * @returns 最新权益快照
 */
export async function pollEntitlement(
  beforeRemaining: number,
  attempts = 6
): Promise<EntitlementPayload> {
  let latest = (await entitlement()).entitlement;
  for (let index = 0; index < attempts; index += 1) {
    if (latest.remaining_quota > beforeRemaining) {
      return latest;
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 1500));
    latest = (await entitlement()).entitlement;
  }
  // 到点还没到账：不本地补额度，如实告诉用户「稍后自动到账」
  wx.showToast({ title: '到账稍有延迟，稍后会自动加上', icon: 'none' });
  return latest;
}

/** 金额格式化：分 -> 元（整数分是唯一传输口径）。 */
export function fenToYuan(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`;
}
