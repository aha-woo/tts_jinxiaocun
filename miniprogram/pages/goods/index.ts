/**
 * 货品页：支持「传统多列表格 / 高密度卡片」一键切换 + 汇总条 + 表头排序 + 点进独立详情页。
 */

import { ApiError, listProductGaps, queryStock } from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { isWideScreen } from '../../utils/screen';

type Filter = 'all' | 'negative' | 'low' | 'nocost';
type ViewMode = 'table' | 'card';
type SortKey = 'default' | 'name' | 'stock' | 'cost' | 'sale' | 'value';
type SortDir = 'asc' | 'desc';

const VIEW_MODE_KEY = 'vi.goods.view_mode';

export interface Row {
  id: string;
  name: string;
  spec: string;
  stock: string;
  stockNum: number | null;
  unit: string;
  costFen: number | null;
  costValText: string;
  costText: string;
  saleFen: number | null;
  saleValText: string;
  valueFen: number | null;
  valueText: string;
  batchModeText: string;
  low: boolean;
  lowThreshold: number;
  negative: boolean;
  trackBatch: boolean;
  missingCost: boolean;
  costHidden: boolean;
  raw: string;
}

interface GoodsData {
  wide: boolean;
  fontSize: 'big' | 'std';
  viewMode: ViewMode;
  sortKey: SortKey;
  sortDir: SortDir;
  keyword: string;
  filter: Filter;
  loading: boolean;
  rows: Row[];
  shown: Row[];
  shownCount: number;
  shownIssueCount: number;
  shownBatchCount: number;
  totalValueText: string;
  errorText: string;
  total: number;
  negativeCount: number;
  gapsCount: number;
  costHidden: boolean;
}

function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed === '') {
      return null;
    }
    const n = Number(trimmed.replace(/[^\d.-]/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
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

function formatStock(stock: number | null): string {
  if (stock === null) {
    return '—';
  }
  return Number.isInteger(stock) ? String(stock) : String(Number(stock.toFixed(4)));
}

function readStoredViewMode(): ViewMode {
  try {
    const saved = wx.getStorageSync(VIEW_MODE_KEY);
    return saved === 'card' ? 'card' : 'table';
  } catch {
    return 'table';
  }
}

/** 把一条库存记录映射成一行；尊重成本可见性开关与负库存标记。 */
export function toGoodsRow(
  item: unknown,
  index: number,
  gapIds?: Set<string>,
  globalCostHidden = false
): Row {
  const raw = item === null || item === undefined ? '' : JSON.stringify(item);
  if (item === null || typeof item !== 'object') {
    return {
      id: `r${index}`,
      name: text(item) || '（未知）',
      spec: '',
      stock: '',
      stockNum: null,
      unit: '',
      costFen: null,
      costValText: '—',
      costText: '',
      saleFen: null,
      saleValText: '—',
      valueFen: null,
      valueText: '—',
      batchModeText: '均价',
      low: false,
      lowThreshold: 5,
      negative: false,
      trackBatch: false,
      missingCost: false,
      costHidden: globalCostHidden,
      raw,
    };
  }
  const o = item as Record<string, unknown>;
  const id = text(o['product_id']) || text(o['id']) || `r${index}`;
  const baseName = text(o['product_name']) || text(o['name']) || text(o['goods_name']) || text(o['title']) || `货品 ${index + 1}`;
  const spec = text(o['spec']);
  const name = spec !== '' && !baseName.includes(spec) ? `${baseName} ${spec}` : baseName;
  const stock = num(o['qty_base']) ?? num(o['stock']) ?? num(o['qty']) ?? num(o['quantity']) ?? num(o['left']);
  const unit = text(o['base_unit']) || text(o['unit']) || text(o['u']);
  const costHidden = Boolean(o['cost_hidden']) || globalCostHidden;
  const trackBatch = Boolean(o['track_batch']);

  const avgCostFen = num(o['avg_cost_fen']);
  const lastPurchaseFen = num(o['last_purchase_price_fen']);
  const effectiveCostFen =
    avgCostFen !== null && avgCostFen > 0
      ? Math.round(avgCostFen)
      : lastPurchaseFen !== null && lastPurchaseFen > 0
        ? Math.round(lastPurchaseFen)
        : null;

  const saleRawFen = num(o['last_sale_price_fen']);
  const saleFen = saleRawFen !== null && saleRawFen >= 0 ? Math.round(saleRawFen) : null;
  const saleValText = saleFen !== null && saleFen > 0 ? `¥${formatYuan(saleFen)}` : '—';

  const missingCost = !costHidden && (gapIds ? gapIds.has(id) : effectiveCostFen === null);
  const lowThreshold = num(o['low_stock_qty']) ?? num(o['low']) ?? num(o['low_stock']) ?? 5;
  const stockNum = stock ?? 0;
  const negative = Boolean(o['negative_flag']) || (stock !== null && stockNum < 0);

  const costValText = costHidden
    ? '仅老板可见'
    : missingCost || effectiveCostFen === null
      ? '待补'
      : `¥${formatYuan(effectiveCostFen)}`;

  const costText = costHidden
    ? '进价仅老板可见'
    : missingCost
      ? '进价待补（不计入毛利）'
      : effectiveCostFen !== null
        ? `均价 ¥${formatYuan(effectiveCostFen)}/${unit || '件'}`
        : '进价已录';

  let valueFen: number | null = null;
  let valueText = '—';
  if (!costHidden && stock !== null && stock >= 0 && effectiveCostFen !== null && effectiveCostFen > 0) {
    valueFen = Math.round(stock * effectiveCostFen);
    valueText = `¥${formatYuan(valueFen, true)}`;
  }

  return {
    id,
    name,
    spec,
    stock: formatStock(stock),
    stockNum: stock,
    unit,
    costFen: costHidden ? null : effectiveCostFen,
    costValText,
    costText,
    saleFen,
    saleValText,
    valueFen,
    valueText,
    batchModeText: trackBatch ? '分批' : '均价',
    low: stock !== null && stockNum <= lowThreshold,
    lowThreshold,
    negative,
    trackBatch,
    missingCost,
    costHidden,
    raw: `名称：${name}\n库存：${formatStock(stock)} ${unit}\n批次管理：${trackBatch ? '已开启' : '未开启'}\n状态：${negative ? '负库存待补进货' : costText}`,
  };
}

function compareNullableNum(a: number | null, b: number | null, dir: SortDir): number {
  if (a === null && b === null) {
    return 0;
  }
  if (a === null) {
    return 1;
  }
  if (b === null) {
    return -1;
  }
  return dir === 'asc' ? a - b : b - a;
}

Page<GoodsData, WechatMiniprogram.Page.CustomOption>({
  data: {
    wide: false,
    fontSize: 'big',
    viewMode: readStoredViewMode(),
    sortKey: 'default',
    sortDir: 'asc',
    keyword: '',
    filter: 'all',
    loading: true,
    rows: [],
    shown: [],
    shownCount: 0,
    shownIssueCount: 0,
    shownBatchCount: 0,
    totalValueText: '¥0',
    errorText: '',
    total: 0,
    negativeCount: 0,
    gapsCount: 0,
    costHidden: false,
  },

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 1 });
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
      const [payload, gaps] = await Promise.all([queryStock('', false, 200), listProductGaps()]);
      const costHidden = Boolean((payload as { cost_hidden?: boolean }).cost_hidden) || Boolean(gaps.cost_hidden);
      const gapIds = new Set((gaps.items ?? []).map((g) => g.product_id));
      const rows = (payload.items ?? []).map((item, index) => toGoodsRow(item, index, gapIds, costHidden));
      this.setData({
        rows,
        total: rows.length,
        negativeCount: rows.filter((r) => r.negative).length,
        gapsCount: costHidden ? 0 : (gaps.items ?? []).length,
        costHidden,
        loading: false,
      });
      this.applyFilter();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : String(error);
      this.setData({
        loading: false,
        errorText: message,
        rows: [],
        shown: [],
        shownCount: 0,
        shownIssueCount: 0,
        shownBatchCount: 0,
        totalValueText: '¥0',
      });
    }
  },

  applyFilter(): void {
    const { rows, filter, keyword, sortKey, sortDir } = this.data;
    const byTab =
      filter === 'negative'
        ? rows.filter((row) => row.negative)
        : filter === 'low'
          ? rows.filter((row) => row.low)
          : filter === 'nocost'
            ? rows.filter((row) => row.missingCost)
            : rows;
    const kw = keyword.trim().toLowerCase();
    const filtered =
      kw === ''
        ? byTab.slice()
        : byTab.filter((row) => row.name.toLowerCase().includes(kw) || row.costText.toLowerCase().includes(kw));

    if (sortKey !== 'default') {
      filtered.sort((a, b) => {
        if (sortKey === 'name') {
          const cmp = a.name.localeCompare(b.name, 'zh-CN');
          return sortDir === 'asc' ? cmp : -cmp;
        }
        if (sortKey === 'stock') {
          return compareNullableNum(a.stockNum, b.stockNum, sortDir);
        }
        if (sortKey === 'cost') {
          return compareNullableNum(a.costFen, b.costFen, sortDir);
        }
        if (sortKey === 'sale') {
          return compareNullableNum(a.saleFen, b.saleFen, sortDir);
        }
        if (sortKey === 'value') {
          return compareNullableNum(a.valueFen, b.valueFen, sortDir);
        }
        return 0;
      });
    }

    let totalValueFen = 0;
    let shownIssueCount = 0;
    let shownBatchCount = 0;
    for (const row of filtered) {
      if (row.negative || row.low) {
        shownIssueCount += 1;
      }
      if (row.trackBatch) {
        shownBatchCount += 1;
      }
      if (row.valueFen !== null) {
        totalValueFen += row.valueFen;
      }
    }

    this.setData({
      shown: filtered,
      shownCount: filtered.length,
      shownIssueCount,
      shownBatchCount,
      totalValueText: `¥${formatYuan(totalValueFen, true)}`,
    });
  },

  onSwitchView(event: WechatMiniprogram.TouchEvent): void {
    const mode = String(event.currentTarget.dataset['mode'] ?? 'table') as ViewMode;
    if (mode !== 'table' && mode !== 'card') {
      return;
    }
    if (mode === this.data.viewMode) {
      return;
    }
    try {
      wx.setStorageSync(VIEW_MODE_KEY, mode);
    } catch {
      // 忽略本地缓存异常
    }
    this.setData({ viewMode: mode });
  },

  onSort(event: WechatMiniprogram.TouchEvent): void {
    const key = String(event.currentTarget.dataset['key'] ?? 'default') as SortKey;
    let sortDir: SortDir = key === 'name' ? 'asc' : 'desc';
    if (this.data.sortKey === key) {
      sortDir = this.data.sortDir === 'asc' ? 'desc' : 'asc';
    }
    this.setData({ sortKey: key, sortDir }, () => this.applyFilter());
  },

  onSearchInput(event: WechatMiniprogram.Input): void {
    const keyword = String(event.detail?.value ?? '');
    this.setData({ keyword }, () => this.applyFilter());
  },

  onClearSearch(): void {
    this.setData({ keyword: '' }, () => this.applyFilter());
  },

  onFilter(event: WechatMiniprogram.TouchEvent): void {
    const filter = String(event.currentTarget.dataset['filter'] ?? 'all') as Filter;
    this.setData({ filter }, () => this.applyFilter());
  },

  onStartCount(): void {
    wx.navigateTo({ url: '/pages/count/index' });
  },

  onGoGaps(): void {
    wx.navigateTo({ url: '/pages/pending/index' });
  },

  onOpen(event: WechatMiniprogram.TouchEvent): void {
    const id = String(event.currentTarget.dataset['id'] ?? '');
    if (id === '' || id.startsWith('r')) {
      return;
    }
    wx.navigateTo({ url: `/pages/goods-detail/index?id=${encodeURIComponent(id)}` });
  },
});
