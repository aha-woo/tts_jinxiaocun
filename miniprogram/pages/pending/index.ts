/**
 * 待处理（原型 vPending）：
 * - 没确认：卡在、只差点确认；
 * - 要补：卡上有缺项（服务端在条目里给出 gaps），点「补 X」直接填这一项，
 *   填完只重算这张卡，不打开整张卡。
 */

import {
  ApiError,
  cancelCard,
  editCard,
  getCard,
  listPending,
  listUnifiedGaps,
  patchProductPrice,
  resolveUnifiedGap,
  reviveCard,
  type PendingCardRow,
  type PendingGap,
  type UnifiedGapRow,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { appendNumericKey } from '../../utils/format';
import type { SheetActionItem } from '../../components/action-sheet/action-sheet';

interface PendingItem {
  cardId: string;
  title: string;
  said: string;
  statusText: string;
  expired: boolean;
  gaps: PendingGap[];
}

interface UnifiedGapView {
  gapId: string;
  kind: string;
  refEntity: string;
  refId: string;
  title: string;
  kindLabel: string;
  actionLabel: string;
}

interface EditingGap {
  cardId?: string;
  productId?: string;
  gapId?: string;
  code: string;
  label: string;
  unit: string;
}

interface PendingData {
  loading: boolean;
  items: PendingItem[];
  shown: PendingItem[];
  ledgerGaps: UnifiedGapView[];
  errorText: string;
  tab: 'unconf' | 'fill';
  unconfCount: number;
  fillCount: number;
  editing: EditingGap | null;
  keypad: string;
  sheet: {
    visible: boolean;
    title: string;
    subtitle: string;
    actions: SheetActionItem[];
  };
}

/** 待确认列表项（服务端摘要）→ 展示项。 */
export function toPendingItem(row: PendingCardRow): PendingItem {
  const title = row.intent_id.startsWith('purchase.')
    ? '进货'
    : row.intent_id.startsWith('sales.')
      ? '卖货'
      : '待处理';
  const expired = row.status === 'expired';
  return {
    cardId: row.card_id,
    title,
    said: row.asr_text ?? '',
    statusText: expired ? '已超时 · 未记账' : '待确认',
    expired,
    gaps: row.gaps ?? [],
  };
}

/** 这些槽位是文字（客户/供应商），其余按数字键盘填。 */
function isTextGap(code: string): boolean {
  return code === 'S' || code === 'C';
}

function gapKindText(kind: string): { label: string; action: string } {
  switch (kind) {
    case 'missing_cost':
      return { label: '缺进价', action: '补进价 ›' };
    case 'negative_stock':
      return { label: '负库存', action: '去补货/已处理 ›' };
    case 'unit_ratio':
      return { label: '缺换算', action: '去设换算 ›' };
    case 'missing_customer':
      return { label: '缺往来', action: '标记已处理 ›' };
    default:
      return { label: '要补', action: '标记已处理 ›' };
  }
}

Page<PendingData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    items: [],
    shown: [],
    ledgerGaps: [],
    errorText: '',
    tab: 'unconf',
    unconfCount: 0,
    fillCount: 0,
    editing: null,
    keypad: '',
    sheet: { visible: false, title: '', subtitle: '', actions: [] },
  },

  sheetCardId: '',

  onShow() {
    void this.load();
  },

  onTab(event: WechatMiniprogram.TouchEvent): void {
    const tab = String(event.currentTarget.dataset['tab'] ?? 'unconf') as 'unconf' | 'fill';
    this.setData({ tab }, () => this.applyTab());
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const [payload, unified] = await Promise.all([listPending(), listUnifiedGaps()]);
      const items = (payload.items ?? []).map((row) => toPendingItem(row));
      const ledgerGaps = (unified.items ?? []).map((row: UnifiedGapRow) => {
        const meta = gapKindText(row.kind);
        return {
          gapId: row.gap_id,
          kind: row.kind,
          refEntity: row.ref_entity,
          refId: row.ref_id,
          title: row.title,
          kindLabel: meta.label,
          actionLabel: meta.action,
        };
      });
      this.setData({ items, ledgerGaps, loading: false });
      this.applyTab();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  applyTab(): void {
    const unconf = this.data.items.filter((item) => item.gaps.length === 0);
    const fill = this.data.items.filter((item) => item.gaps.length > 0);
    this.setData({
      shown: this.data.tab === 'fill' ? fill : unconf,
      unconfCount: unconf.length,
      fillCount: fill.length + this.data.ledgerGaps.length,
    });
  },

  /** 点条目：打开整张卡。 */
  async onOpen(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const cardId = String(event.currentTarget.dataset['card'] ?? '');
    if (cardId === '') {
      return;
    }
    try {
      // 先复活：重写 TTL 并复用同一 session（不重复扣次），避免打开就显示「超时」
      await reviveCard(cardId);
      await getCard(cardId);
      wx.navigateTo({ url: `/pages/card/index?cardId=${cardId}` });
    } catch (error) {
      this.setData({ errorText: error instanceof ApiError ? error.message : String(error) });
    }
  },

  /** 点「补 X」：数字项弹键盘，文字项弹输入框。 */
  onFillStart(event: WechatMiniprogram.TouchEvent): void {
    const cardId = String(event.currentTarget.dataset['card'] ?? '');
    const code = String(event.currentTarget.dataset['code'] ?? '');
    const label = String(event.currentTarget.dataset['label'] ?? '');
    const unit = String(event.currentTarget.dataset['unit'] ?? '');
    const item = this.data.items.find((row) => row.cardId === cardId);
    const gap = item?.gaps.find((row) => row.code === code);
    if (gap === undefined) {
      return;
    }
    if (isTextGap(code)) {
      wx.showModal({
        title: `补 ${label}`,
        editable: true,
        placeholderText: `输入${label}`,
        content: gap.current,
        success: (res) => {
          if (res.confirm) {
            void this.submitGap(cardId, code, String(res.content ?? '').trim());
          }
        },
      });
      return;
    }
    this.setData({ editing: { cardId, code, label, unit }, keypad: gap.current ?? '' });
  },

  onKey(event: WechatMiniprogram.TouchEvent): void {
    const key = String(event.currentTarget.dataset['key'] ?? '');
    this.setData({ keypad: appendNumericKey(this.data.keypad, key, 8) });
  },

  onLedgerGapTap(event: WechatMiniprogram.TouchEvent): void {
    const gapId = String(event.currentTarget.dataset['gap'] ?? '');
    const gap = this.data.ledgerGaps.find((item) => item.gapId === gapId);
    if (gap === undefined) {
      return;
    }
    if (gap.kind === 'missing_cost' && gap.refEntity === 'product') {
      this.setData({
        editing: { productId: gap.refId, gapId: gap.gapId, code: 'COST', label: `${gap.title}（元）`, unit: '元' },
        keypad: '',
      });
      return;
    }
    if (gap.kind === 'unit_ratio') {
      wx.navigateTo({ url: '/pages/goods/index' });
      return;
    }
    wx.showModal({
      title: gap.kindLabel,
      content: `${gap.title}。如果已经确认或处理好了，可以标记为已解决。`,
      confirmText: '标为已处理',
      cancelText: '先留着',
      success: (res) => {
        if (res.confirm) {
          void resolveUnifiedGap(gap.gapId).then(() => {
            wx.showToast({ title: '已处理', icon: 'success' });
            void this.load();
          });
        }
      },
    });
  },

  onKeyOk(): void {
    const editing = this.data.editing;
    if (editing === null || this.data.keypad === '') {
      return;
    }
    if (editing.productId !== undefined && editing.code === 'COST') {
      const fen = Math.round(Number(this.data.keypad) * 100);
      if (!Number.isFinite(fen) || fen < 0) {
        wx.showToast({ title: '请输入合法价格', icon: 'none' });
        return;
      }
      void patchProductPrice(editing.productId, { last_purchase_price_fen: fen }).then(
        () => {
          this.setData({ editing: null, keypad: '' });
          wx.showToast({ title: '进价已补齐', icon: 'success' });
          void this.load();
        },
        (error: unknown) => {
          this.setData({ errorText: String(error), editing: null });
        }
      );
      return;
    }
    if (editing.cardId !== undefined) {
      void this.submitGap(editing.cardId, editing.code, this.data.keypad);
    }
  },

  onKeyClose(): void {
    this.setData({ editing: null, keypad: '' });
  },

  async submitGap(cardId: string, code: string, value: string): Promise<void> {
    if (value === '') {
      return;
    }
    try {
      await editCard(cardId, [{ code, value }]);
      this.setData({ editing: null, keypad: '' });
      wx.showToast({ title: '补好了', icon: 'none' });
      await this.load();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message, editing: null });
    }
  },

  /** 长按：丢弃这张卡。 */
  onDiscardMenu(event: WechatMiniprogram.TouchEvent): void {
    const cardId = String(event.currentTarget.dataset['card'] ?? '');
    if (cardId === '') {
      return;
    }
    this.sheetCardId = cardId;
    this.setData({
      sheet: {
        visible: true,
        title: '处理这张待确认卡',
        subtitle: '丢弃后不会入账，也不会改变库存与账款',
        actions: [{ key: 'discard', label: '丢弃这张卡（不记账）', tone: 'danger' }],
      },
    });
  },

  onSheetClose(): void {
    this.sheetCardId = '';
    this.setData({ 'sheet.visible': false });
  },

  onSheetSelect(event: WechatMiniprogram.CustomEvent<{ index: number; key: string }>): void {
    const cardId = this.sheetCardId;
    const { key } = event.detail;
    this.onSheetClose();
    if (key === 'discard' && cardId !== '') {
      void this.discard(cardId);
    }
  },

  async discard(cardId: string): Promise<void> {
    try {
      await cancelCard(cardId);
      await this.load();
    } catch (error) {
      this.setData({ errorText: error instanceof ApiError ? error.message : String(error) });
    }
  },
});
