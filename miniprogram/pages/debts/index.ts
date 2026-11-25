/**
 * 欠款页（原型 vDebt）：别人欠我 / 我欠别人 + 合计 + 每人一行。
 * 数据来自只读 `/query/debt`；方向字段做尽力解析，缺失时归到「别人欠我」。
 */

import { ApiError, queryDebt } from '../../services/api';
import { ensureLogin } from '../../services/auth';

type Dir = 'in' | 'out';

interface Row {
  id: string;
  name: string;
  amount: number;
  amountText: string;
  last: string;
  dir: Dir;
  raw: string;
}

interface DebtsData {
  tab: Dir;
  loading: boolean;
  rows: Row[];
  shown: Row[];
  sumText: string;
  errorText: string;
}

function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string') {
    const n = Number(value.replace(/[^\d.-]/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
}

export function toDebtRow(item: unknown, index: number): Row {
  if (item === null || typeof item !== 'object') {
    return { id: `d${index}`, name: text(item) || '（未知）', amount: 0, amountText: '', last: '', dir: 'in', raw: '' };
  }
  const o = item as Record<string, unknown>;
  const name =
    text(o['partner_name']) ||
    text(o['name']) ||
    text(o['customer']) ||
    text(o['partner']) ||
    `客户 ${index + 1}`;
  const fen = num(o['outstanding_fen']) ?? num(o['amount_fen']);
  const amount = fen !== null ? fen / 100 : (num(o['amount']) ?? num(o['balance']) ?? num(o['debt']) ?? 0);
  const dirRaw = (text(o['dir']) || text(o['direction'])).toLowerCase();
  const dir: Dir = dirRaw === 'out' || dirRaw === 'payable' || dirRaw === 'owe' ? 'out' : 'in';
  const amountText = text(o['outstanding_display']) || `¥${amount.toFixed(2)}`;
  const last = text(o['last']) || text(o['last_date']) || text(o['updated_at']);
  return {
    id: text(o['partner_id']) || text(o['id']) || `d${index}`,
    name,
    amount,
    amountText,
    last,
    dir,
    raw: `${dir === 'in' ? '客户应收' : '供应商应付'}：${amountText}\n最近往来：${last || '近期'}`,
  };
}

Page<DebtsData, WechatMiniprogram.Page.CustomOption>({
  data: { tab: 'in', loading: true, rows: [], shown: [], sumText: '0.00', errorText: '' },

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 2 });
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const payload = await queryDebt('');
      const rows = (payload.items ?? []).map((item, index) => toDebtRow(item, index));
      this.setData({ rows, loading: false });
      this.applyTab();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message, rows: [], shown: [] });
    }
  },

  applyTab(): void {
    const { rows, tab } = this.data;
    const shown = rows.filter((row) => row.dir === tab);
    const sum = shown.reduce((total, row) => total + row.amount, 0);
    this.setData({ shown, sumText: sum.toFixed(2) });
  },

  onTab(event: WechatMiniprogram.TouchEvent): void {
    const tab = String(event.currentTarget.dataset['tab'] ?? 'in') as Dir;
    this.setData({ tab }, () => this.applyTab());
  },

  onOpen(event: WechatMiniprogram.TouchEvent): void {
    const id = String(event.currentTarget.dataset['id'] ?? '');
    const row = this.data.rows.find((item) => item.id === id);
    if (row === undefined) {
      return;
    }
    wx.showModal({ title: row.name, content: row.raw.slice(0, 300), showCancel: false });
  },
});
