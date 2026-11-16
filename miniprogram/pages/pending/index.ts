/**
 * 待确认列表：30 秒没确认的卡片都在这儿。
 *
 * 关键行为（T04 的超时语义）
 * ------------------------
 * - 超时卡片**不落账**，但**不重复扣次**：重新打开复用同一个 `session_id`；
 * - 这里只做「重新打开 → 再确认」，用户点开后走的还是同一张卡的确认接口。
 */

import { ApiError, cancelCard, getCard, listPending, type CardPayload } from '../../services/api';

interface PendingItem {
  cardId: string;
  title: string;
  amountText: string;
  createdAt: string;
  expired: boolean;
  previewVersion: number;
}

interface PendingData {
  loading: boolean;
  items: PendingItem[];
  errorText: string;
}

/** 卡片列表项 → 展示项（金额取 ④ 区明细之和的展示文本）。 */
export function toPendingItem(card: CardPayload, nowMs = Date.now()): PendingItem {
  const total = card.calc.lines.reduce((sum, line) => sum + line.amount_fen, 0);
  const expiresAt = card.meta.expires_at === null ? 0 : Date.parse(card.meta.expires_at);
  return {
    cardId: card.meta.card_id,
    title: `${card.intent.intent_label_zh} · ${card.calc.lines.length} 项`,
    amountText: `¥${(total / 100).toFixed(2)}`,
    createdAt: expiresAt > 0 ? card.meta.expires_at ?? '' : '',
    expired: expiresAt > 0 ? nowMs >= expiresAt : true,
    previewVersion: card.meta.preview_version,
  };
}

Page<PendingData, WechatMiniprogram.Page.CustomOption>({
  data: { loading: true, items: [], errorText: '' },

  onShow() {
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      const payload = await listPending();
      this.setData({ items: payload.items.map((card) => toPendingItem(card)), loading: false });
    } catch (error) {
      const message =
        error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  /** 打开卡片（服务端会重新只读预演，版本以最新为准）。 */
  async onOpen(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const cardId = String(event.currentTarget.dataset['card'] ?? '');
    if (cardId === '') {
      return;
    }
    try {
      await getCard(cardId); // 先确认卡片还在（顺便触发一次只读预演）
      wx.navigateTo({ url: `/pages/card/index?cardId=${cardId}` });
    } catch (error) {
      this.setData({ errorText: error instanceof ApiError ? error.message : String(error) });
    }
  },

  /** 丢弃这张（不落账）。 */
  async onDiscard(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const cardId = String(event.currentTarget.dataset['card'] ?? '');
    try {
      await cancelCard(cardId);
      await this.load();
    } catch (error) {
      this.setData({ errorText: error instanceof ApiError ? error.message : String(error) });
    }
  },
});
