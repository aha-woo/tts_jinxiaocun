/**
 * 货品详情页：概览指标 + 档案参数管理（统一数字键盘编辑）+ 分批/合并管理 + 在库批次 + 出入库流水。
 */

import {
  ApiError,
  getProductDetail,
  listBatches,
  mergeProduct,
  patchProductPrice,
  queryStock,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { isWideScreen } from '../../utils/screen';
import type { SheetActionItem } from '../../components/action-sheet/action-sheet';

interface BatchItemView {
  batchId: string;
  titleText: string;
  qtyText: string;
  unitCostText: string;
  expiryDate: string;
  statusText: string;
  isVirtual: boolean;
  isOpen: boolean;
}

interface MovementItemView {
  key: string;
  dateText: string;
  reasonText: string;
  qtyText: string;
  isIn: boolean;
  subText: string;
}

interface MergeCandidate {
  id: string;
  name: string;
  stockDisplay: string;
}

interface GoodsDetailData {
  wide: boolean;
  loading: boolean;
  errorText: string;
  productId: string;
  name: string;
  spec: string;
  baseUnit: string;
  status: string;
  isDraft: boolean;
  negative: boolean;
  low: boolean;
  trackBatch: boolean;
  costHidden: boolean;
  missingCost: boolean;
  stockDisplay: string;
  avgCostText: string;
  salePriceText: string;
  stockValueText: string;
  stockValueWarn: boolean;
  purchaseDisplayText: string;
  saleDisplayText: string;
  lowStockDisplayText: string;
  saleRaw: string;
  lowStockRaw: string;
  aliases: string[];
  unitConversions: string[];
  batches: BatchItemView[];
  movements: MovementItemView[];
  kp: {
    visible: boolean;
    field: 'purchase' | 'sale' | 'low' | '';
    title: string;
    subtitle: string;
    value: string;
    unit: string;
    confirmText: string;
  };
  sheet: {
    visible: boolean;
    title: string;
    subtitle: string;
    actions: SheetActionItem[];
  };
}

const REASON_LABELS: Record<string, string> = {
  purchase: '进货',
  sales: '卖货',
  sale: '卖货',
  sales_return: '销退',
  purchase_return: '采退',
  count_adjust: '盘点',
  loss: '报损',
  opening: '期初',
  reversal: '撤销',
  oversold_settle: '补货结算',
  transfer_in: '调入',
  transfer_out: '调出',
};

function trimDecimal(raw: string | number | null | undefined): string {
  if (raw === null || raw === undefined) {
    return '';
  }
  const s = String(raw).trim();
  if (!s.includes('.')) {
    return s;
  }
  return s.replace(/0+$/, '').replace(/\.$/, '') || '0';
}

function formatYuan(fen: number, withCommas = false): string {
  const raw = (fen / 100).toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
  if (!withCommas) {
    return raw;
  }
  const parts = raw.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.join('.');
}

function fenText(fen: number | null | undefined, fallback: string, withCommas = false): string {
  if (fen === null || fen === undefined) {
    return fallback;
  }
  return `¥${formatYuan(fen, withCommas)}`;
}

Page<GoodsDetailData, WechatMiniprogram.Page.CustomOption>({
  data: {
    wide: false,
    loading: true,
    errorText: '',
    productId: '',
    name: '',
    spec: '',
    baseUnit: '件',
    status: 'active',
    isDraft: false,
    negative: false,
    low: false,
    trackBatch: false,
    costHidden: false,
    missingCost: false,
    stockDisplay: '—',
    avgCostText: '—',
    salePriceText: '—',
    stockValueText: '—',
    stockValueWarn: false,
    purchaseDisplayText: '—',
    saleDisplayText: '未设',
    lowStockDisplayText: '5 件（默认）',
    saleRaw: '',
    lowStockRaw: '',
    aliases: [],
    unitConversions: [],
    batches: [],
    movements: [],
    kp: {
      visible: false,
      field: '',
      title: '',
      subtitle: '',
      value: '',
      unit: '',
      confirmText: '保存',
    },
    sheet: { visible: false, title: '', subtitle: '', actions: [] },
  },

  productId: '',
  mergeCandidates: [] as MergeCandidate[],

  onLoad(options: Record<string, string | undefined>) {
    this.productId = decodeURIComponent(String(options['id'] ?? '')).trim();
    this.setData({ wide: isWideScreen(), productId: this.productId });
    if (this.productId === '') {
      this.setData({ loading: false, errorText: '缺少货品 ID' });
    }
  },

  onShow() {
    this.setData({ wide: isWideScreen() });
    if (this.productId !== '') {
      void this.load();
    }
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
  },

  async load(): Promise<void> {
    if (this.productId === '') {
      return;
    }
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const [detail, batchRes] = await Promise.all([
        getProductDetail(this.productId),
        listBatches(this.productId),
      ]);
      const p = detail.product;
      const inv = detail.inventory;
      const costHidden = Boolean(detail.cost_hidden);
      const spec = String(p.spec ?? '');
      const baseUnit = p.base_unit || '件';
      const fullName = spec !== '' && !p.name.includes(spec) ? `${p.name} ${spec}` : p.name;
      const qtyNum = Number(inv.qty_base);
      const lowLimit = p.low_stock_qty ? Number(p.low_stock_qty) : 5;
      const negative = Boolean(inv.negative_flag) || (Number.isFinite(qtyNum) && qtyNum < 0);
      const low = Number.isFinite(qtyNum) && qtyNum <= (Number.isFinite(lowLimit) ? lowLimit : 5);
      const effectiveCostFen = inv.avg_cost_fen ?? p.last_purchase_price_fen ?? null;
      const missingCost = !costHidden && (p.last_purchase_price_fen === null || p.last_purchase_price_fen === 0);

      let stockValueText = '—';
      let stockValueWarn = false;
      if (negative) {
        stockValueText = '待补货';
        stockValueWarn = true;
      } else if (!costHidden && inv.stock_value_fen !== null && inv.stock_value_fen >= 0) {
        stockValueText = fenText(inv.stock_value_fen, '—', true);
      }

      const batches: BatchItemView[] = (batchRes.items ?? []).map((b) => {
        const label = b.label || (b.is_virtual ? '未到货' : '在库批次');
        const statusText = b.is_virtual
          ? '待补进货'
          : b.status === 'open'
            ? '在库'
            : b.status === 'exhausted'
              ? '已用完'
              : '已调整';
        const unit = b.base_unit || baseUnit;
        return {
          batchId: b.batch_id,
          titleText: label,
          qtyText: `进 ${trimDecimal(b.qty_in)}${unit} · 剩 ${trimDecimal(b.qty_left)}${unit}`,
          unitCostText: costHidden ? '成本已隐藏' : fenText(b.unit_cost_fen, '进价待补'),
          expiryDate: b.expiry_date || '',
          statusText,
          isVirtual: Boolean(b.is_virtual),
          isOpen: b.status === 'open',
        };
      });

      const movements: MovementItemView[] = (detail.movements ?? []).map((m, idx) => {
        const num = Number(m.qty_base);
        const isIn = Number.isFinite(num) ? num > 0 : false;
        const absText = trimDecimal(m.qty_base);
        const qtyText = `${isIn && !absText.startsWith('+') ? '+' : ''}${absText}${m.unit || baseUnit}`;
        const rawReason = m.reason || m.doc_type || '';
        const reasonText = REASON_LABELS[rawReason] || REASON_LABELS[m.doc_type] || rawReason || '变动';
        const dateText = m.biz_date ? m.biz_date.slice(5) : '—';
        const costPart =
          !costHidden && m.unit_cost_fen !== null && m.unit_cost_fen !== undefined
            ? ` · ¥${formatYuan(m.unit_cost_fen)}`
            : '';
        return {
          key: `${m.doc_id || rawReason}-${idx}`,
          dateText,
          reasonText,
          qtyText,
          isIn,
          subText: `${m.doc_no || '手工调整'}${costPart}`,
        };
      });

      const unitConversions = (detail.unit_conversions ?? []).map(
        (u) => `1 ${u.from_unit} = ${u.ratio} ${u.to_unit}`
      );
      const saleRaw =
        p.last_sale_price_fen !== null && p.last_sale_price_fen !== undefined
          ? formatYuan(p.last_sale_price_fen)
          : '';
      const lowStockRaw = p.low_stock_qty ? trimDecimal(p.low_stock_qty) : '';

      this.setData({
        loading: false,
        name: fullName,
        spec,
        baseUnit,
        status: p.status,
        isDraft: p.status === 'draft',
        negative,
        low,
        trackBatch: Boolean(p.track_batch),
        costHidden,
        missingCost,
        stockDisplay: inv.qty_display,
        avgCostText: costHidden ? '仅老板可见' : fenText(effectiveCostFen, '待补'),
        salePriceText: fenText(p.last_sale_price_fen, '未设'),
        stockValueText,
        stockValueWarn,
        purchaseDisplayText: costHidden
          ? '仅老板可见'
          : missingCost
            ? '进价待补'
            : `${fenText(effectiveCostFen, '—')}/${baseUnit}`,
        saleDisplayText: saleRaw !== '' ? `¥${saleRaw}/${baseUnit}` : '未设',
        lowStockDisplayText: lowStockRaw !== '' ? `${lowStockRaw} ${baseUnit}` : `5 ${baseUnit}（默认）`,
        saleRaw,
        lowStockRaw,
        aliases: detail.aliases ?? [],
        unitConversions,
        batches,
        movements,
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onEditPurchase(): void {
    if (this.data.costHidden || !this.data.missingCost) {
      return;
    }
    this.setData({
      kp: {
        visible: true,
        field: 'purchase',
        title: '补录参考进价',
        subtitle: '仅用于补齐毛利缺口，不改历史进货批次',
        value: '',
        unit: `元/${this.data.baseUnit}`,
        confirmText: '保存进价',
      },
    });
  },

  onEditSale(): void {
    this.setData({
      kp: {
        visible: true,
        field: 'sale',
        title: '修改默认售价',
        subtitle: '仅作下次开单默认价，不影响历史单据',
        value: this.data.saleRaw,
        unit: `元/${this.data.baseUnit}`,
        confirmText: '保存售价',
      },
    });
  },

  onEditLow(): void {
    this.setData({
      kp: {
        visible: true,
        field: 'low',
        title: '修改低库存预警线',
        subtitle: '库存低于此值时在列表提醒，不影响账务',
        value: this.data.lowStockRaw,
        unit: this.data.baseUnit,
        confirmText: '保存预警线',
      },
    });
  },

  onKpChange(event: WechatMiniprogram.CustomEvent<{ value: string }>): void {
    this.setData({ 'kp.value': String(event.detail?.value ?? '') });
  },

  onKpClose(): void {
    this.setData({ 'kp.visible': false });
  },

  async onKpConfirm(event: WechatMiniprogram.CustomEvent<{ value: string }>): Promise<void> {
    const field = this.data.kp.field;
    const raw = String(event.detail?.value ?? '').trim();
    this.onKpClose();
    if (this.productId === '' || field === '') {
      return;
    }
    if (raw === '') {
      wx.showToast({ title: '请先输入数值', icon: 'none' });
      return;
    }
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) {
      wx.showToast({ title: '输入格式不正确', icon: 'none' });
      return;
    }
    const body: {
      last_purchase_price_fen?: number;
      last_sale_price_fen?: number;
      low_stock_qty?: number | null;
    } = {};
    if (field === 'purchase') {
      if (n <= 0) {
        wx.showToast({ title: '进价需大于 0', icon: 'none' });
        return;
      }
      body.last_purchase_price_fen = Math.round(n * 100);
    } else if (field === 'sale') {
      body.last_sale_price_fen = Math.round(n * 100);
    } else if (field === 'low') {
      body.low_stock_qty = n;
    }
    try {
      await patchProductPrice(this.productId, body);
      wx.showToast({ title: '已保存', icon: 'success' });
      await this.load();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      wx.showToast({ title: message, icon: 'none' });
    }
  },

  async onToggleBatch(): Promise<void> {
    if (this.productId === '') {
      return;
    }
    const next = !this.data.trackBatch;
    try {
      await patchProductPrice(this.productId, { track_batch: next });
      wx.showToast({ title: next ? '已开启分批管理' : '已切回移动平均', icon: 'none' });
      await this.load();
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      wx.showToast({ title: message, icon: 'none' });
    }
  },

  async onOpenMerge(): Promise<void> {
    try {
      const res = await queryStock('', false, 200);
      const candidates: MergeCandidate[] = [];
      for (const item of res.items ?? []) {
        if (!item || typeof item !== 'object') {
          continue;
        }
        const o = item as Record<string, unknown>;
        const id = String(o['product_id'] ?? '');
        if (!id || id === this.productId) {
          continue;
        }
        const baseName = String(o['product_name'] ?? o['name'] ?? '');
        const spec = String(o['spec'] ?? '');
        const name = spec !== '' && !baseName.includes(spec) ? `${baseName} ${spec}` : baseName;
        const stockDisplay = String(o['qty_display'] ?? `${o['qty_base'] ?? '0'}${o['base_unit'] ?? ''}`);
        candidates.push({ id, name, stockDisplay });
        if (candidates.length >= 15) {
          break;
        }
      }
      if (candidates.length === 0) {
        wx.showToast({ title: '暂无可合并的其它货品', icon: 'none' });
        return;
      }
      this.mergeCandidates = candidates;
      this.setData({
        sheet: {
          visible: true,
          title: `把「${this.data.name}」合并到哪件货？`,
          subtitle: '合并后库存、流水并入目标货，原名自动保留为别名',
          actions: candidates.map((c) => ({
            key: c.id,
            label: c.name,
            desc: `当前库存 ${c.stockDisplay}`,
          })),
        },
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      wx.showToast({ title: message, icon: 'none' });
    }
  },

  onSheetClose(): void {
    this.setData({ 'sheet.visible': false });
  },

  onSheetSelect(event: WechatMiniprogram.CustomEvent<{ index: number; key: string }>): void {
    const targetId = String(event.detail?.key ?? '');
    this.onSheetClose();
    const candidates = this.mergeCandidates as MergeCandidate[];
    const target = candidates.find((c: MergeCandidate) => c.id === targetId);
    if (!target) {
      return;
    }
    wx.showModal({
      title: '确认合并重复商品',
      content: `要把「${this.data.name}」的库存、流水和别名全部并入「${target.name}」吗？`,
      confirmText: '确认合并',
      success: (cf) => {
        if (!cf.confirm) {
          return;
        }
        void mergeProduct(this.productId, target.id).then(
          () => {
            wx.showToast({ title: `已合并入 ${target.name}`, icon: 'success' });
            setTimeout(() => {
              wx.navigateBack();
            }, 600);
          },
          (err: unknown) => {
            const msg = err instanceof ApiError ? err.message : String(err);
            wx.showModal({ title: '暂不能合并', content: msg, showCancel: false });
          }
        );
      },
    });
  },
});
