import {
  confirmDeliveryNote,
  DeliveryNoteDraft,
  DeliveryNoteLine,
  parseDeliveryNote,
} from '../../services/api';

interface EditableLine extends DeliveryNoteLine {
  unit_price_yuan: string;
  amount_yuan: string;
  unit_ratio_input: string;
}

interface DeliveryNoteData {
  loading: boolean;
  submitting: boolean;
  errorText: string;
  previewImagePath: string;
  draft: DeliveryNoteDraft | null;
  supplierName: string;
  docDate: string;
  paymentStatus: '已结' | '未结';
  statedTotalYuan: string;
  computedTotalYuan: string;
  lines: EditableLine[];
}

function fenToYuanText(fen: number | null | undefined): string {
  if (fen === null || fen === undefined || !Number.isFinite(fen)) {
    return '';
  }
  const v = fen / 100;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

function yuanTextToFen(text: string): number | null {
  const cleaned = text.trim();
  if (cleaned === '') {
    return null;
  }
  const num = Number(cleaned);
  if (!Number.isFinite(num) || num < 0) {
    return null;
  }
  return Math.round(num * 100);
}

Page<DeliveryNoteData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: false,
    submitting: false,
    errorText: '',
    previewImagePath: '',
    draft: null,
    supplierName: '',
    docDate: '',
    paymentStatus: '已结',
    statedTotalYuan: '',
    computedTotalYuan: '0',
    lines: [],
  },

  onChooseImage(): void {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        const file = res.tempFiles?.[0];
        if (!file?.tempFilePath) {
          return;
        }
        this.setData({ previewImagePath: file.tempFilePath, errorText: '', loading: true });
        void this.compressAndParse(file.tempFilePath);
      },
      fail: (err) => {
        if (err.errMsg && !err.errMsg.includes('cancel')) {
          this.setData({ errorText: err.errMsg });
        }
      },
    });
  },

  async compressAndParse(rawPath: string): Promise<void> {
    try {
      const compressedPath = await new Promise<string>((resolve) => {
        wx.compressImage({
          src: rawPath,
          quality: 50,
          success: (r) => resolve(r.tempFilePath || rawPath),
          fail: () => resolve(rawPath),
        });
      });
      const base64 = await new Promise<string>((resolve, reject) => {
        wx.getFileSystemManager().readFile({
          filePath: compressedPath,
          encoding: 'base64',
          success: (r) => resolve(String(r.data ?? '')),
          fail: (e) => reject(new Error(e.errMsg || '读取照片失败')),
        });
      });

      let payload: { image_base64?: string; object_ref?: string };
      if (base64.length <= 75000) {
        payload = { image_base64: base64, object_ref: `local://${Date.now()}.jpg` };
      } else if (typeof wx.cloud?.uploadFile === 'function') {
        const uploaded = await wx.cloud.uploadFile({
          cloudPath: `delivery_notes/${Date.now()}.jpg`,
          filePath: compressedPath,
        });
        payload = { object_ref: uploaded.fileID };
      } else {
        this.setData({
          loading: false,
          errorText: '这张进货单照片稍微大了点，请离远半步重拍一张（更省流量、识别更快）',
        });
        return;
      }

      const draft = await parseDeliveryNote(payload);
      this.applyDraft(draft);
    } catch (error) {
      this.setData({
        errorText: error instanceof Error ? error.message : String(error),
      });
    } finally {
      this.setData({ loading: false });
    }
  },

  applyDraft(draft: DeliveryNoteDraft): void {
    const lines: EditableLine[] = (draft.lines ?? []).map((line) => ({
      ...line,
      unit_price_yuan: fenToYuanText(line.unit_price_fen),
      amount_yuan: fenToYuanText(line.amount_fen),
      unit_ratio_input: line.unit_ratio ?? '',
    }));
    this.setData({
      draft,
      supplierName: draft.supplier_name ?? '',
      docDate: draft.doc_date ?? '',
      paymentStatus: draft.payment_status === '未结' ? '未结' : '已结',
      statedTotalYuan: fenToYuanText(draft.stated_total_fen),
      computedTotalYuan: fenToYuanText(draft.computed_total_fen) || '0',
      lines,
    });
  },

  recalcTotals(lines: EditableLine[]): void {
    let totalFen = 0;
    for (const line of lines) {
      const amtFen = yuanTextToFen(line.amount_yuan);
      if (amtFen !== null) {
        totalFen += amtFen;
      }
    }
    this.setData({ lines, computedTotalYuan: fenToYuanText(totalFen) || '0' });
  },

  onHeaderInput(event: WechatMiniprogram.Input): void {
    const field = String(event.currentTarget.dataset.field ?? '');
    const value = String(event.detail.value ?? '');
    if (field === 'supplierName') {
      this.setData({ supplierName: value });
    } else if (field === 'docDate') {
      this.setData({ docDate: value });
    } else if (field === 'statedTotalYuan') {
      this.setData({ statedTotalYuan: value });
    }
  },

  onTogglePaymentStatus(event: WechatMiniprogram.TouchEvent): void {
    const ps = event.currentTarget.dataset.ps === '未结' ? '未结' : '已结';
    this.setData({ paymentStatus: ps });
  },

  onLineInput(event: WechatMiniprogram.Input): void {
    const index = Number(event.currentTarget.dataset.index ?? -1);
    const field = String(event.currentTarget.dataset.field ?? '');
    const value = String(event.detail.value ?? '');
    const lines = [...this.data.lines];
    const row = lines[index];
    if (!row) {
      return;
    }
    const updated: EditableLine = { ...row };
    if (field === 'product_name') {
      updated.product_name = value;
    } else if (field === 'unit') {
      updated.unit = value;
    } else if (field === 'quantity') {
      const q = Number(value);
      updated.quantity = Number.isFinite(q) && q > 0 ? q : 0;
      const upFen = yuanTextToFen(updated.unit_price_yuan);
      if (upFen !== null && updated.quantity > 0) {
        updated.amount_yuan = fenToYuanText(Math.round(upFen * updated.quantity));
      }
    } else if (field === 'unit_price_yuan') {
      updated.unit_price_yuan = value;
      const upFen = yuanTextToFen(value);
      if (upFen !== null && updated.quantity > 0) {
        updated.amount_yuan = fenToYuanText(Math.round(upFen * updated.quantity));
      }
    } else if (field === 'amount_yuan') {
      updated.amount_yuan = value;
    } else if (field === 'unit_ratio_input') {
      updated.unit_ratio_input = value;
    }
    lines[index] = updated;
    this.recalcTotals(lines);
  },

  onAddEmptyLine(): void {
    const lines = [
      ...this.data.lines,
      {
        product_name: '',
        unit: '箱',
        quantity: 1,
        unit_price_fen: null,
        amount_fen: null,
        unit_price_yuan: '',
        amount_yuan: '',
        unit_ratio_input: '',
        is_new_product: true,
        needs_unit_ratio: false,
      },
    ];
    this.recalcTotals(lines);
  },

  onRemoveLine(event: WechatMiniprogram.TouchEvent): void {
    const index = Number(event.currentTarget.dataset.index ?? -1);
    if (this.data.lines.length <= 1) {
      wx.showToast({ title: '至少保留一行', icon: 'none' });
      return;
    }
    const lines = this.data.lines.filter((_, i) => i !== index);
    this.recalcTotals(lines);
  },

  async onConfirm(): Promise<void> {
    if (!this.data.draft) {
      return;
    }
    const payloadLines = this.data.lines
      .filter((row) => row.product_name.trim() !== '' && row.quantity > 0)
      .map((row) => ({
        product_name: row.product_name.trim(),
        unit: row.unit.trim() || '件',
        quantity: row.quantity,
        unit_price_fen: yuanTextToFen(row.unit_price_yuan),
        amount_fen: yuanTextToFen(row.amount_yuan),
        matched_product_id: row.matched_product_id ?? null,
        unit_ratio: row.unit_ratio_input.trim() || null,
      }));
    if (payloadLines.length === 0) {
      this.setData({ errorText: '请至少保留一行有效商品和数量' });
      return;
    }

    this.setData({ submitting: true, errorText: '' });
    try {
      const res = await confirmDeliveryNote(this.data.draft.attachment_id, {
        supplier_name: this.data.supplierName.trim() || null,
        doc_date: this.data.docDate.trim() || null,
        payment_status: this.data.paymentStatus,
        stated_total_fen: yuanTextToFen(this.data.statedTotalYuan),
        lines: payloadLines,
      });
      wx.showToast({ title: `已入库 ${res.doc_no}`, icon: 'success' });
      setTimeout(() => {
        wx.navigateBack({ delta: 1 });
      }, 600);
    } catch (error) {
      this.setData({ errorText: error instanceof Error ? error.message : String(error) });
    } finally {
      this.setData({ submitting: false });
    }
  },
});
