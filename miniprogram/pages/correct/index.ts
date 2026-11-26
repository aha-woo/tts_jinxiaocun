/**
 * 一步改单卡：在同一事务内自动反做原单并落账更正单。
 */

import {
  ApiError,
  correctDoc,
  getDocDetail,
  reverseDoc,
  type DocBrief,
  type DocLineDetail,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';

interface EditLine {
  productId: string;
  productName: string;
  qty: string;
  unit: string;
  price: string;
  beforeQty: string;
  beforePrice: string;
}

interface CorrectData {
  docId: string;
  loading: boolean;
  doc: DocBrief | null;
  dirLabel: string;
  paymentLabel: string;
  statusLabel: string;
  lines: EditLine[];
  submitting: boolean;
  errorText: string;
  note: string;
}

const PAYMENT_LABELS: Record<string, string> = {
  paid: '已付清',
  unpaid: '赊账未结',
  partial: '部分结算',
  已结: '已付清',
  未结: '赊账未结',
  部分: '部分结算',
};

const STATUS_LABELS: Record<string, string> = {
  confirmed: '已入账',
  posted: '已入账',
  reversed: '已撤销',
  draft: '草稿',
};

function fenToYuan(fen: number | null): string {
  if (fen === null || fen === undefined) {
    return '';
  }
  return (fen / 100).toFixed(2).replace(/\.00$/, '');
}

Page<CorrectData, WechatMiniprogram.Page.CustomOption>({
  data: {
    docId: '',
    loading: true,
    doc: null,
    dirLabel: '',
    paymentLabel: '',
    statusLabel: '',
    lines: [],
    submitting: false,
    errorText: '',
    note: '一步改好：原单保留并自动反做冲销，更正后的新单同步生效。',
  },

  onLoad(query: Record<string, string | undefined>) {
    const docId = String(query['docId'] ?? '');
    this.setData({ docId });
    if (docId === '') {
      this.setData({ loading: false, errorText: '没带单号，回首页流水里点一张单' });
      return;
    }
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const detail = await getDocDetail(this.data.docId);
      const doc = detail.doc;
      const isSales = doc.intent_id.startsWith('sales');
      this.setData({
        doc,
        dirLabel: isSales ? '卖货' : doc.intent_id.startsWith('purchase') ? '进货' : '单据',
        paymentLabel: PAYMENT_LABELS[doc.payment_status] ?? doc.payment_status,
        statusLabel: STATUS_LABELS[doc.status] ?? doc.status,
        lines: detail.lines.map((line: DocLineDetail) => ({
          productId: line.product_id,
          productName: line.product_name || '货品',
          qty: line.qty,
          unit: line.unit,
          price: fenToYuan(line.unit_price_fen),
          beforeQty: line.qty,
          beforePrice: fenToYuan(line.unit_price_fen),
        })),
        loading: false,
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onLineInput(event: WechatMiniprogram.Input): void {
    const index = Number(event.currentTarget.dataset['index']);
    const field = String(event.currentTarget.dataset['field'] ?? 'qty');
    const lines = this.data.lines.map((line, i) =>
      i === index ? { ...line, [field]: event.detail.value } : line
    );
    this.setData({ lines });
  },

  /** 一步原子改单：校验输入 → 确认弹窗 → POST /docs/{id}/correct。 */
  async onSubmitCorrect(): Promise<void> {
    const doc = this.data.doc;
    if (doc === null || this.data.lines.length === 0) {
      this.setData({ errorText: '这张单没有明细，不能改' });
      return;
    }
    const payloadLines: Array<{
      product_id: string;
      qty: number;
      unit: string;
      unit_price_fen?: number | null;
    }> = [];
    for (const line of this.data.lines) {
      const qtyNum = Number(line.qty);
      if (!Number.isFinite(qtyNum) || qtyNum <= 0) {
        this.setData({ errorText: `${line.productName} 的数量必须大于 0` });
        return;
      }
      const priceTrimmed = line.price.trim();
      let priceFen: number | null | undefined;
      if (priceTrimmed !== '') {
        const yuan = Number(priceTrimmed);
        if (!Number.isFinite(yuan) || yuan < 0) {
          this.setData({ errorText: `${line.productName} 的单价格式不对` });
          return;
        }
        priceFen = Math.round(yuan * 100);
      } else {
        priceFen = null;
      }
      payloadLines.push({
        product_id: line.productId,
        qty: qtyNum,
        unit: line.unit,
        unit_price_fen: priceFen,
      });
    }

    const confirmed = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '确认一步更正？',
        content: `原单 ${doc.doc_no} 将自动反做并生成更正后的新单。`,
        confirmText: '确认更正',
        success: (res) => resolve(Boolean(res.confirm)),
        fail: () => resolve(false),
      });
    });
    if (!confirmed) {
      return;
    }
    this.setData({ submitting: true, errorText: '' });
    try {
      const resp = await correctDoc(this.data.docId, {
        lines: payloadLines,
        reason: '卡片一步更正',
      });
      wx.showToast({ title: `已更正为 ${resp.result.new_doc_no}`, icon: 'none' });
      setTimeout(() => wx.navigateBack(), 900);
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
    } finally {
      this.setData({ submitting: false });
    }
  },

  /** 只撤销，不重记。 */
  async onReverseOnly(): Promise<void> {
    const confirmed = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '确认只撤销不重记？',
        content: '撤销后原单保留并标为已撤，库存与账款自动冲回。',
        confirmText: '确认撤销',
        success: (res) => resolve(Boolean(res.confirm)),
        fail: () => resolve(false),
      });
    });
    if (!confirmed) {
      return;
    }
    this.setData({ submitting: true, errorText: '' });
    try {
      await reverseDoc(this.data.docId, '改单：只撤销');
      wx.showToast({ title: '已撤销（原单保留）', icon: 'none' });
      setTimeout(() => wx.navigateBack(), 800);
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ errorText: message });
    } finally {
      this.setData({ submitting: false });
    }
  },
});
