/**
 * 商品档案要补：列出缺进价的商品，点一条直接补价。
 * 商品档案是进销存的基础数据——没有进价，毛利永远是错的。
 */

import { ApiError, listProductGaps, patchProductPrice, type ProductGap } from '../../services/api';
import { ensureLogin } from '../../services/auth';

interface GapsData {
  loading: boolean;
  items: ProductGap[];
  errorText: string;
}

Page<GapsData, WechatMiniprogram.Page.CustomOption>({
  data: { loading: true, items: [], errorText: '' },

  onShow() {
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const payload = await listProductGaps();
      this.setData({ items: payload.items ?? [], loading: false });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onFill(event: WechatMiniprogram.TouchEvent): void {
    const productId = String(event.currentTarget.dataset['id'] ?? '');
    const name = String(event.currentTarget.dataset['name'] ?? '');
    if (productId === '') {
      return;
    }
    wx.showModal({
      title: `补 ${name} 的进价`,
      editable: true,
      placeholderText: '每件多少钱（元）',
      success: (res) => {
        if (!res.confirm) {
          return;
        }
        const raw = String(res.content ?? '').trim();
        const yuan = Number(raw);
        if (!Number.isFinite(yuan) || yuan <= 0) {
          wx.showToast({ title: '请输入大于 0 的金额', icon: 'none' });
          return;
        }
        void this.save(productId, Math.round(yuan * 100));
      },
    });
  },

  async save(productId: string, fen: number): Promise<void> {
    try {
      await patchProductPrice(productId, { last_purchase_price_fen: fen });
      wx.showToast({ title: '补好了', icon: 'none' });
      await this.load();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
    }
  },
});
