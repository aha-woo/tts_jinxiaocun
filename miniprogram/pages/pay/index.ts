/**
 * 套餐与购买（**付费入口只在小程序内**，电脑版微信也能买；没有网页版、没有账号密码）。
 *
 * 两个口径必须**分开显示**（架构 §8.4「计费双口径」）
 * ---------------------------------------------
 * - 用量：`usage_quota.period = YYYY-MM`，**自然月**，回答「这个月用了多少」；
 * - 权益：**30 天滚动窗口**，回答「这张卡还剩几天 / 还剩多少次」。
 *
 * 这两块**不得拼进同一个进度条或同一句文案**，也不得互相推导——
 * 混口径会让用户算出「我明明还有次数却不能用」这类投诉。
 *
 * 发货铁律：前端 `success` **不发货**，只轮询权益；额度由微信支付服务端推送后到账。
 */

import {
  ApiError,
  entitlement,
  listGoods,
  usage,
  type EntitlementPayload,
  type UsagePayload,
} from '../../services/api';
import { canPurchaseHere, pollEntitlement, purchase } from '../../services/virtual-pay';
import { ensureLogin } from '../../services/auth';

interface GoodsView {
  goodsId: string;
  name: string;
  priceText: string;
  desc: string;
}

interface PayData {
  loading: boolean;
  usage: UsagePayload | null;
  entitlement: EntitlementPayload | null;
  goods: GoodsView[];
  canBuyHere: boolean;
  paying: string;
  message: string;
  errorText: string;
}

Page<PayData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    usage: null,
    entitlement: null,
    goods: [],
    canBuyHere: true,
    paying: '',
    message: '',
    errorText: '',
  },

  onShow() {
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const [usagePayload, entitlementPayload, goodsPayload] = await Promise.all([
        usage(),
        entitlement(),
        listGoods(),
      ]);
      this.setData({
        // 两个口径分别来自各自的包络体，端上**不合并、不互相推导**
        usage: {
          ...usagePayload.usage,
          used: Number(usagePayload.usage?.used ?? 0),
          quota: Number(usagePayload.usage?.quota ?? 0),
        },
        entitlement: {
          ...entitlementPayload.entitlement,
          remaining_quota: Number(entitlementPayload.entitlement?.remaining_quota ?? 0),
          days_left: Number(entitlementPayload.entitlement?.days_left ?? 0),
        },
        goods: goodsPayload.items.map((item) => ({
          goodsId: item.goods_id,
          name: item.name,
          priceText: item.price_text,
          desc: item.desc,
        })),
        canBuyHere: canPurchaseHere(),
        loading: false,
      });
    } catch (error) {
      this.setData({ loading: false, errorText: this.messageOf(error) });
    }
  },

  /** 购买：下单 → 拉起支付 → **只轮询权益，不本地加额度**。 */
  async onBuy(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const goodsId = String(event.currentTarget.dataset['goods'] ?? '');
    if (goodsId === '' || this.data.entitlement === null) {
      return;
    }
    const before = this.data.entitlement.remaining_quota;
    this.setData({ paying: goodsId, message: '', errorText: '' });
    try {
      const result = await purchase(goodsId);
      if (!result.paid) {
        this.setData({ paying: '', message: result.message });
        return;
      }
      this.setData({ message: '支付完成，正在等到账（额度由微信服务端推送后加上）' });
      const latest = await pollEntitlement(before);
      this.setData({ entitlement: latest, paying: '' });
      await this.load();
    } catch (error) {
      this.setData({ paying: '', errorText: this.messageOf(error) });
    }
  },

  messageOf(error: unknown): string {
    if (error instanceof ApiError) {
      return `${error.message}${error.errorCode ? `（${error.errorCode}）` : ''}`;
    }
    return error instanceof Error ? error.message : String(error);
  },
});
