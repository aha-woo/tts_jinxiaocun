/**
 * 手动记账（**全程脱离语音**）：选商品 → 填数量 → 填单价 → 选方向 → 生成卡片。
 *
 * 实现说明
 * -------
 * 服务端只有一条建卡入口：`POST /voice/sessions/{id}/parse`，它接受**已转好的文本**
 * （`asr_text`）。所以手动模式把点选结果拼成一句规范话再送进去——
 * 这样「语音」和「键盘」走的是**同一条解析与落账链路**，不存在第二条写账路径
 * （否则手工会绕过影响预演和 CAS，那是最危险的分叉）。
 */

import { ApiError, createVoiceSession, parseSession } from '../../services/api';

/** 把点选结果拼成一句规范话（与口语同构，便于规则层复用）。 */
export function composeUtterance(input: {
  direction: 'in' | 'out';
  product: string;
  quantity: string;
  price: string;
}): string {
  const verb = input.direction === 'in' ? '进货' : '卖货';
  const parts = [verb, input.product];
  if (input.quantity !== '') {
    parts.push(`${input.quantity}件`);
  }
  if (input.price !== '') {
    parts.push(`单价${input.price}元`);
  }
  return parts.join('，');
}

interface ManualData {
  product: string;
  quantity: string;
  price: string;
  direction: 'in' | 'out';
  products: string[];
  submitting: boolean;
  errorText: string;
  preview: string;
}

Page<ManualData, WechatMiniprogram.Page.CustomOption>({
  data: {
    product: '',
    quantity: '',
    price: '',
    direction: 'in',
    products: ['可乐', '娃哈哈', '红牛', '矿泉水'],
    submitting: false,
    errorText: '',
    preview: '',
  },

  onLoad() {
    this.setData({ preview: composeUtterance({ direction: 'in', product: '', quantity: '', price: '' }) });
  },

  onProduct(event: WechatMiniprogram.CustomEvent<{ product: string }>): void {
    this.setData({ product: event.detail.product }, () => this.refreshPreview());
  },

  onDirection(event: WechatMiniprogram.CustomEvent<{ direction: 'in' | 'out' }>): void {
    this.setData({ direction: event.detail.direction }, () => this.refreshPreview());
  },

  onInput(event: WechatMiniprogram.CustomEvent<{ field: 'quantity' | 'price'; value: string }>): void {
    const patch: Record<string, string> = {};
    patch[event.detail.field] = event.detail.value;
    this.setData(patch, () => this.refreshPreview());
  },

  refreshPreview(): void {
    this.setData({
      preview: composeUtterance({
        direction: this.data.direction,
        product: this.data.product,
        quantity: this.data.quantity,
        price: this.data.price,
      }),
    });
  },

  /** 提交：建会话（0 音频）→ 用拼好的文本解析 → 有卡片就跳确认页。 */
  async onSubmit(): Promise<void> {
    if (this.data.product === '' || this.data.quantity === '') {
      this.setData({ errorText: '商品和数量要先填上' });
      return;
    }
    this.setData({ submitting: true, errorText: '' });
    try {
      const session = await createVoiceSession({ duration_ms: 0, mode: 'tap', source: 'manual' });
      const parsed = await parseSession(session.session_id, {
        asr_text: this.data.preview,
        asr_confidence: 1.0,
        duration_ms: 0,
      });
      if (parsed.card !== null && parsed.card !== undefined) {
        wx.redirectTo({ url: `/pages/card/index?cardId=${parsed.card.meta.card_id}` });
        return;
      }
      if (parsed.clarify !== null && parsed.clarify !== undefined) {
        wx.redirectTo({ url: `/pages/card/index?sessionId=${session.session_id}` });
        return;
      }
      this.setData({ errorText: `没生成卡片（${parsed.route}）` });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? `${error.message}${error.errorCode ? `（${error.errorCode}）` : ''}`
          : String(error);
      this.setData({ errorText: message });
    } finally {
      this.setData({ submitting: false });
    }
  },
});
