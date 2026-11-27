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
  getMe,
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
  badgeText: string;
  priceText: string;
  desc: string;
}

interface PayData {
  loading: boolean;
  isBoss: boolean;
  usage: UsagePayload | null;
  entitlement: EntitlementPayload | null;
  goods: GoodsView[];
  canBuyHere: boolean;
  paying: string;
  message: string;
  errorText: string;
}

function formatDateOnly(raw: unknown): string {
  const s = typeof raw === 'string' ? raw.trim() : '';
  if (s === '') {
    return '长期有效';
  }
  return s.slice(0, 10);
}

function planNameOf(code: string): string {
  const map: Record<string, string> = {
    free: '免费体验版',
    '30day': '30 天权益版',
    quota_pack: '语音加油包',
  };
  return map[code] ?? (code || '免费体验版');
}

function formatFenLabel(fen: unknown): string {
  const n = Number(fen ?? 0);
  if (!Number.isFinite(n) || n <= 0) {
    return '¥0';
  }
  return n % 100 === 0 ? `¥${Math.round(n / 100)}` : `¥${(n / 100).toFixed(2)}`;
}

Page<PayData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    isBoss: false,
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
      const [me, usagePayload, entitlementPayload, goodsPayload] = await Promise.all([
        getMe(),
        usage(),
        entitlement(),
        listGoods(),
      ]);
      const rawUsage = usagePayload.usage;
      const rawEnt = entitlementPayload.entitlement;
      this.setData({
        isBoss: me.role === 'boss',
        // 两个口径分别来自各自的包络体，端上**不合并、不互相推导**
        usage: {
          ...rawUsage,
          used: Number(rawUsage?.used ?? rawUsage?.used_count ?? 0),
          quota: Number(rawUsage?.quota ?? rawUsage?.free_quota ?? 0),
        },
        entitlement: {
          ...rawEnt,
          plan_name: String(rawEnt?.plan_name || planNameOf(String(rawEnt?.plan_code ?? 'free'))),
          remaining_quota: Number(rawEnt?.remaining_quota ?? rawEnt?.remaining ?? 0),
          days_left: Number(rawEnt?.days_left ?? 0),
          valid_until: String(rawEnt?.valid_until_display || formatDateOnly(rawEnt?.valid_until)),
        },
        goods: goodsPayload.items.map((item) => ({
          goodsId: item.goods_id,
          name: String(item.name || item.name_zh || '语音套餐'),
          badgeText:
            item.goods_type === 'quota_pack'
              ? '次数包'
              : Number(item.validity_days ?? 0) > 0
                ? `${Number(item.validity_days)} 天权益`
                : '',
          priceText: String(item.price_text || formatFenLabel(item.price_fen)),
          desc: String(item.desc || item.benefits_zh || ''),
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
    if (this.data.loading || this.data.paying !== '' || !this.data.canBuyHere || !this.data.isBoss) {
      return;
    }
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
      return error.message;
    }
    return error instanceof Error ? error.message : String(error);
  },
});
