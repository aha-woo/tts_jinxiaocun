/**
 * 盘点（接后端）：开盘点 → 快照账面 → 点行填实盘 → 应用差异。
 * 差异会写 count_adjust 流水并调整库存与批次；盘点单只能应用一次。
 */

import {
  ApiError,
  applyCount,
  fillCountLine,
  getCount,
  getOpenCount,
  startCount,
  voiceBatchCount,
  type CountLineRow,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { SpeechGate, type SpeechResult } from '../../services/speech';
import { appendNumericKey } from '../../utils/format';
import { isWideScreen } from '../../utils/screen';

interface CountDisplayLine extends CountLineRow {
  hasDiff: boolean;
  diffPositive: boolean;
  absDiff: string;
}

interface CountData {
  wide: boolean;
  loading: boolean;
  countId: string;
  resumedOpen: boolean;
  recordingVoice: boolean;
  lines: CountDisplayLine[];
  counted: number;
  editing: string;
  keypad: string;
  reason: string;
  reasons: string[];
  applying: boolean;
  errorText: string;
}

Page<CountData, WechatMiniprogram.Page.CustomOption>({
  data: {
    wide: false,
    loading: true,
    countId: '',
    resumedOpen: false,
    recordingVoice: false,
    lines: [],
    counted: 0,
    editing: '',
    keypad: '',
    reason: '正常损耗',
    reasons: ['正常损耗', '漏记卖货', '漏记进货', '拿错货', '赠品'],
    applying: false,
    errorText: '',
  },

  speechGate: null as SpeechGate | null,

  onLoad() {
    this.setData({ wide: isWideScreen() });
    this.speechGate = new SpeechGate();
    this.speechGate.onAutoStop((said: SpeechResult | null) => {
      if (said !== null) {
        void this.applyVoiceBatch(said);
      }
    });
    void this.init(false);
  },

  onShow() {
    this.setData({ wide: isWideScreen() });
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
  },

  onUnload() {
    this.speechGate?.dispose();
  },

  async init(forceNew = false): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      let cid = '';
      let resumed = false;
      if (!forceNew) {
        const existing = await getOpenCount();
        if (existing.count?.count_id) {
          cid = existing.count.count_id;
          resumed = true;
        }
      }
      if (!cid) {
        const started = await startCount();
        cid = started.count_id;
      }
      this.setData({ countId: cid, resumedOpen: resumed });
      await this.refresh();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onRestartCount(): void {
    wx.showModal({
      title: '重开新盘点',
      content: '要重新拉一份当前库存快照从头数吗？',
      success: (res) => {
        if (res.confirm) {
          void this.init(true);
        }
      },
    });
  },

  async onVoiceBatch(): Promise<void> {
    const gate = this.speechGate;
    if (gate === null || !gate.available) {
      wx.showToast({ title: '语音插件暂未就绪，可直接点下面商品行键盘填数', icon: 'none' });
      return;
    }
    if (this.data.recordingVoice) {
      this.setData({ recordingVoice: false });
      const said = await gate.stop();
      if (said !== null) {
        await this.applyVoiceBatch(said);
      }
      return;
    }
    const outcome = await gate.start();
    if (outcome.granted) {
      this.setData({ recordingVoice: true });
    }
  },

  async applyVoiceBatch(said: SpeechResult): Promise<void> {
    this.setData({ recordingVoice: false });
    if (!said.text || !this.data.countId) {
      return;
    }
    try {
      const res = await voiceBatchCount(this.data.countId, said.text);
      await this.refresh();
      const hitNames = res.matched.map((m) => `${m.name}:${m.qty_counted}${m.unit}`).join('、');
      const missHint = res.unmatched.length > 0 ? `\n未识别：${res.unmatched.join('；')}` : '';
      wx.showModal({
        title: `语音录入了 ${res.matched.length} 样`,
        content: `${hitNames || '未匹配到商品'}${missHint}`,
        showCancel: false,
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
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
    this.setData({ editing: productId, keypad: line.qty_counted ?? '', reason: line.reason || '正常损耗' });
  },

  onReason(event: WechatMiniprogram.TouchEvent): void {
    const reason = String(event.currentTarget.dataset['reason'] ?? '正常损耗');
    this.setData({ reason });
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
      await fillCountLine(this.data.countId, this.data.editing, this.data.keypad, this.data.reason);
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
      const derivedTip =
        result.derived_sales_doc_id && Number(result.derived_sales_amount_fen || 0) >= 0
          ? `\n倒推销售已记账：¥${(Number(result.derived_sales_amount_fen || 0) / 100).toFixed(2)}`
          : '';
      wx.showModal({
        title: '盘点已应用',
        content: `调整 ${result.adjusted_lines} 件货，数量差异合计 ${result.total_qty_diff}${derivedTip}`,
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
