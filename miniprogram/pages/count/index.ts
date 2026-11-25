/**
 * 盘点（接后端）：开盘点 → 快照账面 → 点行填实盘 → 应用差异。
 * 差异会写 count_adjust 流水并调整库存与批次；盘点单只能应用一次。
 */

import {
  ApiError,
  applyCount,
  fillCountLine,
  getCount,
  startCount,
  type CountLineRow,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { appendNumericKey } from '../../utils/format';

interface CountDisplayLine extends CountLineRow {
  hasDiff: boolean;
  diffPositive: boolean;
  absDiff: string;
}

interface CountData {
  loading: boolean;
  countId: string;
  lines: CountDisplayLine[];
  counted: number;
  editing: string;
  keypad: string;
  applying: boolean;
  errorText: string;
}

Page<CountData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    countId: '',
    lines: [],
    counted: 0,
    editing: '',
    keypad: '',
    applying: false,
    errorText: '',
  },

  onLoad() {
    void this.init();
  },

  async init(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const started = await startCount();
      this.setData({ countId: started.count_id });
      await this.refresh();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  async refresh(): Promise<void> {
    try {
      const detail = await getCount(this.data.countId);
      const lines: CountDisplayLine[] = detail.lines.map((line) => {
        const diffVal = line.diff !== null ? Number(line.diff) : 0;
        const hasDiff = line.diff !== null && Number.isFinite(diffVal) && diffVal !== 0;
        return {
          ...line,
          hasDiff,
          diffPositive: diffVal > 0,
          absDiff: hasDiff ? String(Math.abs(diffVal)) : '0',
        };
      });
      this.setData({
        lines,
        loading: false,
        counted: lines.filter((line) => line.qty_counted !== null).length,
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onRow(event: WechatMiniprogram.TouchEvent): void {
    const productId = String(event.currentTarget.dataset['product'] ?? '');
    const line = this.data.lines.find((row) => row.product_id === productId);
    if (line === undefined) {
      return;
    }
    this.setData({ editing: productId, keypad: line.qty_counted ?? '' });
  },

  onKey(event: WechatMiniprogram.TouchEvent): void {
    const key = String(event.currentTarget.dataset['key'] ?? '');
    this.setData({ keypad: appendNumericKey(this.data.keypad, key, 8) });
  },

  async onKeyOk(): Promise<void> {
    if (this.data.editing === '' || this.data.keypad === '') {
      return;
    }
    try {
      await fillCountLine(this.data.countId, this.data.editing, this.data.keypad);
      this.setData({ editing: '', keypad: '' });
      await this.refresh();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
    }
  },

  onKeyClose(): void {
    this.setData({ editing: '', keypad: '' });
  },

  async onApply(): Promise<void> {
    const okToApply = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '确认应用盘点',
        content: `已盘点 ${this.data.counted} 样货品，确认后将按实盘数量调整库存。`,
        confirmText: '确认应用',
        success: (res) => resolve(Boolean(res.confirm)),
        fail: () => resolve(false),
      });
    });
    if (!okToApply) {
      return;
    }
    this.setData({ applying: true, errorText: '' });
    try {
      const payload = await applyCount(this.data.countId);
      const result = payload.result;
      wx.showModal({
        title: '盘点已应用',
        content: `调整 ${result.adjusted_lines} 件货，数量差异合计 ${result.total_qty_diff}`,
        showCancel: false,
        success: () => wx.navigateBack({ delta: 1 }),
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
    } finally {
      this.setData({ applying: false });
    }
  },
});
