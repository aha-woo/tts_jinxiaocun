/**
 * 欠款页（原型 vDebt）：别人欠我 / 我欠别人 + 合计 + 每人一行。
 * 数据来自只读 `/query/debt`；方向字段做尽力解析，缺失时归到「别人欠我」。
 */

import { ApiError, getPartnerStatement, queryDebt, settleDebt } from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { isWideScreen } from '../../utils/screen';
import type { SheetActionItem } from '../../components/action-sheet/action-sheet';

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
  wide: boolean;
  fontSize: 'big' | 'std';
  tab: Dir;
  loading: boolean;
  rows: Row[];
  shown: Row[];
  sumText: string;
  inRows: Row[];
  outRows: Row[];
  inSumText: string;
  outSumText: string;
  netSumText: string;
  errorText: string;
  sheet: {
    visible: boolean;
    title: string;
    subtitle: string;
    actions: SheetActionItem[];
  };
  keypad: {
    visible: boolean;
    title: string;
    subtitle: string;
    unit: string;
    value: string;
  };
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
  data: {
    wide: false,
    fontSize: 'big',
    tab: 'in',
    loading: true,
    rows: [],
    shown: [],
    sumText: '0.00',
    inRows: [],
    outRows: [],
    inSumText: '0.00',
    outSumText: '0.00',
    netSumText: '0.00',
    errorText: '',
    sheet: { visible: false, title: '', subtitle: '', actions: [] },
    keypad: { visible: false, title: '', subtitle: '', unit: '元', value: '' },
  },

  sheetCtx: null as
    | { mode: 'row'; row: Row }
    | { mode: 'method'; row: Row; amountFen: number }
    | null,
  keypadRow: null as Row | null,

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 2 });
    const fontSize = (wx.getStorageSync('dsh:fontSize') as string) === 'std' ? 'std' : 'big';
    this.setData({ wide: isWideScreen(), fontSize });
    void this.load();
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
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
      const message = error instanceof ApiError ? error.message : String(error);
      this.setData({ loading: false, errorText: message, rows: [], shown: [] });
    }
  },

  applyTab(): void {
    const { rows, tab } = this.data;
    const inRows = rows.filter((row) => row.dir === 'in');
    const outRows = rows.filter((row) => row.dir === 'out');
    const inSum = inRows.reduce((total, row) => total + row.amount, 0);
    const outSum = outRows.reduce((total, row) => total + row.amount, 0);
    const shown = tab === 'in' ? inRows : outRows;
    const sum = tab === 'in' ? inSum : outSum;
    this.setData({
      shown,
      sumText: sum.toFixed(2),
      inRows,
      outRows,
      inSumText: inSum.toFixed(2),
      outSumText: outSum.toFixed(2),
      netSumText: (inSum - outSum).toFixed(2),
    });
  },

  onQuickSettle(event: WechatMiniprogram.TouchEvent): void {
    const id = String(event.currentTarget.dataset['id'] ?? '');
    const row = this.data.rows.find((item) => item.id === id);
    if (row !== undefined) {
      this.promptSettle(row);
    }
  },

  onQuickStatement(event: WechatMiniprogram.TouchEvent): void {
    const id = String(event.currentTarget.dataset['id'] ?? '');
    const row = this.data.rows.find((item) => item.id === id);
    if (row !== undefined) {
      void this.shareStatementImage(row);
    }
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
    const actionLabel = row.dir === 'in' ? '登记收款核销' : '登记付款核销';
    this.sheetCtx = { mode: 'row', row };
    this.setData({
      sheet: {
        visible: true,
        title: row.name,
        subtitle: `${row.dir === 'in' ? '别人欠我' : '我欠别人'} · ${row.amountText}${row.last ? ` · 最近往来 ${row.last}` : ''}`,
        actions: [
          { key: 'settle', label: actionLabel, desc: '录入本次结清或部分还款金额', tone: 'primary' },
          { key: 'statement', label: '生成对账单图片发微信', desc: '生成明细图，可长按或转发核对' },
          { key: 'detail', label: '查看往来摘要' },
        ],
      },
    });
  },

  onSheetClose(): void {
    this.sheetCtx = null;
    this.setData({ 'sheet.visible': false });
  },

  onSheetSelect(event: WechatMiniprogram.CustomEvent<{ index: number; key: string }>): void {
    const ctx = this.sheetCtx;
    const { key } = event.detail;
    this.onSheetClose();
    if (!ctx) {
      return;
    }
    if (ctx.mode === 'row') {
      if (key === 'settle') {
        this.promptSettle(ctx.row);
      } else if (key === 'statement') {
        void this.shareStatementImage(ctx.row);
      } else if (key === 'detail') {
        wx.showModal({ title: ctx.row.name, content: ctx.row.raw.slice(0, 300), showCancel: false });
      }
      return;
    }
    if (ctx.mode === 'method') {
      void settleDebt({
        partner_id: ctx.row.id,
        direction: ctx.row.dir === 'in' ? 'receivable' : 'payable',
        amount_fen: ctx.amountFen,
        method: key || '微信',
      }).then(
        (done) => {
          wx.showToast({ title: `已核销 ${done.doc_no}`, icon: 'success' });
          void this.load();
        },
        (err: unknown) => {
          const msg = err instanceof ApiError ? err.message : String(err);
          wx.showModal({ title: '核销未成功', content: msg, showCancel: false });
        }
      );
    }
  },

  promptSettle(row: Row): void {
    const defaultYuan = row.amount > 0 ? row.amount.toFixed(2).replace(/\.00$/, '') : '';
    this.keypadRow = row;
    this.setData({
      keypad: {
        visible: true,
        title: `${row.dir === 'in' ? '登记收款' : '登记付款'} · ${row.name}`,
        subtitle: `当前未结 ${row.amountText}`,
        unit: '元',
        value: defaultYuan,
      },
    });
  },

  onKeypadChange(event: WechatMiniprogram.CustomEvent<{ value: string }>): void {
    this.setData({ 'keypad.value': String(event.detail?.value ?? '') });
  },

  onKeypadClose(): void {
    this.keypadRow = null;
    this.setData({ 'keypad.visible': false });
  },

  onKeypadConfirm(event: WechatMiniprogram.CustomEvent<{ value: string }>): void {
    const row = this.keypadRow;
    const raw = String(event.detail?.value ?? this.data.keypad.value).trim();
    const numVal = Number(raw);
    if (row === null || !Number.isFinite(numVal) || numVal <= 0) {
      wx.showToast({ title: '金额必须是大于 0 的数字', icon: 'none' });
      return;
    }
    this.keypadRow = null;
    this.setData({ 'keypad.visible': false });
    const amountFen = Math.round(numVal * 100);
    const amountYuan = (amountFen / 100).toFixed(2).replace(/\.00$/, '');
    this.sheetCtx = { mode: 'method', row, amountFen };
    this.setData({
      sheet: {
        visible: true,
        title: `选择${row.dir === 'in' ? '收款' : '付款'}方式`,
        subtitle: `${row.name} · 本次 ¥${amountYuan}`,
        actions: [
          { key: '微信', label: '微信收付款', tone: 'primary' },
          { key: '现金', label: '现金' },
          { key: '银行卡/支付宝', label: '银行卡 / 支付宝' },
        ],
      },
    });
  },

  async shareStatementImage(row: Row): Promise<void> {
    wx.showLoading({ title: '生成对账单中…' });
    try {
      const dir = row.dir === 'in' ? 'receivable' : 'payable';
      const stmt = await getPartnerStatement(row.id, dir);
      const imgPath = await this.drawStatementCanvas(stmt);
      wx.hideLoading();
      wx.previewImage({
        urls: [imgPath],
        current: imgPath,
      });
    } catch (error) {
      wx.hideLoading();
      const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
      wx.showModal({ title: '生成对账单失败', content: msg, showCancel: false });
    }
  },

  drawStatementCanvas(stmt: {
    store_name: string;
    partner_name: string;
    direction: string;
    generated_at: string;
    total_outstanding_fen: number;
    items: Array<{ doc_no: string; biz_date: string; outstanding_fen: number }>;
  }): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const query = wx.createSelectorQuery();
      query
        .select('#stmtCanvas')
        .fields({ node: true, size: true })
        .exec((res) => {
          const entry = Array.isArray(res) ? (res[0] as { node?: unknown } | undefined) : undefined;
          const canvas = entry?.node as
            | {
                width: number;
                height: number;
                getContext: (t: '2d') => {
                  fillStyle: string;
                  strokeStyle: string;
                  font: string;
                  fillRect: (x: number, y: number, w: number, h: number) => void;
                  fillText: (text: string, x: number, y: number) => void;
                  beginPath: () => void;
                  moveTo: (x: number, y: number) => void;
                  lineTo: (x: number, y: number) => void;
                  stroke: () => void;
                } | null;
              }
            | undefined;
          if (!canvas) {
            reject(new Error('当前环境不支持绘制对账单图片'));
            return;
          }
          const shownItems = stmt.items.slice(0, 10);
          const overflowCount = Math.max(0, stmt.items.length - shownItems.length);
          const w = 640;
          const h = Math.max(520, 260 + shownItems.length * 46 + (overflowCount > 0 ? 44 : 0));
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          if (!ctx) {
            reject(new Error('Canvas 上下文初始化失败'));
            return;
          }
          ctx.fillStyle = '#f5f1e8';
          ctx.fillRect(0, 0, w, h);
          ctx.fillStyle = '#1f1b16';
          ctx.font = 'bold 28px sans-serif';
          ctx.fillText(`${stmt.store_name} · 往来对账单`, 36, 56);
          ctx.font = '20px sans-serif';
          ctx.fillStyle = '#5c5347';
          ctx.fillText(`对象：${stmt.partner_name}（${stmt.direction === 'receivable' ? '应收货款' : '应付货款'}）`, 36, 98);
          ctx.fillText(`日期：${stmt.generated_at}`, 36, 130);

          ctx.strokeStyle = '#d8d0c2';
          ctx.beginPath();
          ctx.moveTo(36, 152);
          ctx.lineTo(w - 36, 152);
          ctx.stroke();

          ctx.fillStyle = '#b45309';
          ctx.font = 'bold 30px sans-serif';
          ctx.fillText(`未结合计：¥${(stmt.total_outstanding_fen / 100).toFixed(2)}`, 36, 196);

          ctx.fillStyle = '#1f1b16';
          ctx.font = '20px sans-serif';
          let y = 242;
          for (const item of shownItems) {
            ctx.fillText(`${item.biz_date}  ${item.doc_no}`, 36, y);
            ctx.fillText(`¥${(item.outstanding_fen / 100).toFixed(2)}`, w - 180, y);
            y += 42;
          }
          if (overflowCount > 0) {
            ctx.fillStyle = '#7c7367';
            ctx.font = '18px sans-serif';
            ctx.fillText(`另有 ${overflowCount} 笔未结明细（合计已包含全部）`, 36, y + 6);
          }

          wx.canvasToTempFilePath({
            canvas: canvas as unknown as WechatMiniprogram.Canvas,
            success: (out) => resolve(out.tempFilePath),
            fail: (err) => reject(new Error(err.errMsg || '导出图片失败')),
          });
        });
    });
  },
});
