/**
 * 货品页：库存大字 + 负库存/分批角标 + 快没了/缺进价/负库存筛选 + 一键开关批次。
 */

import { ApiError, listProductGaps, mergeProduct, patchProductPrice, queryStock } from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { isWideScreen } from '../../utils/screen';

type Filter = 'all' | 'negative' | 'low' | 'nocost';

interface Row {
  id: string;
  name: string;
  stock: string;
  unit: string;
  costText: string;
  low: boolean;
  negative: boolean;
  trackBatch: boolean;
  missingCost: boolean;
  costHidden: boolean;
  raw: string;
}

interface GoodsData {
  wide: boolean;
  keyword: string;
  filter: Filter;
  loading: boolean;
  rows: Row[];
  shown: Row[];
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
    const n = Number(value.replace(/[^\d.-]/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
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
      stock: '',
      unit: '',
      costText: '',
      low: false,
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
  const costFen = num(o['avg_cost_fen']) ?? num(o['last_purchase_price_fen']);
  const costYuan = costFen !== null && costFen > 0 ? (costFen / 100).toFixed(2).replace(/\.00$/, '') : null;
  const missingCost = !costHidden && (gapIds ? gapIds.has(id) : costYuan === null);
  const lowLimit = num(o['low']) ?? num(o['low_stock']);
  const stockNum = stock ?? 0;
  const negative = Boolean(o['negative_flag']) || (stock !== null && stockNum < 0);
  const costText = costHidden
    ? '进价仅老板可见'
    : missingCost
      ? '进价待补（不计入毛利）'
      : costYuan !== null
        ? `均价 ¥${costYuan}/${unit || '件'}`
        : '进价已录';
  return {
    id,
    name,
    stock: stock === null ? '—' : String(stock),
    unit,
    costText,
    low: stock !== null && stockNum <= (lowLimit ?? 5),
    negative,
    trackBatch,
    missingCost,
    costHidden,
    raw: `名称：${name}\n库存：${stock === null ? '—' : stock} ${unit}\n批次管理：${trackBatch ? '已开启' : '未开启'}\n状态：${negative ? '负库存待补进货' : costText}`,
  };
}

Page<GoodsData, WechatMiniprogram.Page.CustomOption>({
  data: {
    wide: false,
    keyword: '',
    filter: 'all',
    loading: true,
    rows: [],
    shown: [],
    errorText: '',
    total: 0,
    negativeCount: 0,
    gapsCount: 0,
    costHidden: false,
  },

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 1 });
    this.setData({ wide: isWideScreen() });
    void this.load();
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const [payload, gaps] = await Promise.all([queryStock(''), listProductGaps()]);
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
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message, rows: [], shown: [] });
    }
  },

  applyFilter(): void {
    const { rows, filter, keyword } = this.data;
    const byTab =
      filter === 'negative'
        ? rows.filter((row) => row.negative)
        : filter === 'low'
          ? rows.filter((row) => row.low)
          : filter === 'nocost'
            ? rows.filter((row) => row.missingCost)
            : rows;
    const kw = keyword.trim().toLowerCase();
    const shown =
      kw === ''
        ? byTab
        : byTab.filter((row) => row.name.toLowerCase().includes(kw) || row.costText.toLowerCase().includes(kw));
    this.setData({ shown });
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
    const row = this.data.rows.find((item) => item.id === id);
    if (row === undefined) {
      return;
    }
    const batchLabel = row.trackBatch ? '关闭按批次管理' : '开启按批次管理（水产/生鲜）';
    const others = this.data.rows.filter((r) => r.id !== row.id && !r.id.startsWith('r'));
    const items = others.length > 0 ? ['查看货品详情', batchLabel, '合并到另一件重复货'] : ['查看货品详情', batchLabel];
    wx.showActionSheet({
      itemList: items,
      success: async (res) => {
        if (res.tapIndex === 0) {
          wx.showModal({ title: row.name, content: row.raw.slice(0, 300), showCancel: false });
          return;
        }
        if (res.tapIndex === 1) {
          try {
            await patchProductPrice(row.id, { track_batch: !row.trackBatch });
            wx.showToast({ title: !row.trackBatch ? '已开启分批管理' : '已切回移动平均', icon: 'none' });
            void this.load();
          } catch (error) {
            const msg = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
            wx.showToast({ title: msg, icon: 'none' });
          }
          return;
        }
        if (res.tapIndex === 2 && others.length > 0) {
          const candidates = others.slice(0, 6);
          wx.showActionSheet({
            itemList: candidates.map((c) => `合并入：${c.name}（${c.stock}${c.unit}）`),
            success: (pick) => {
              const target = candidates[pick.tapIndex];
              if (!target) {
                return;
              }
              wx.showModal({
                title: '确认合并重复商品',
                content: `要把「${row.name}」的库存、流水和别名全部并入「${target.name}」吗？合并后原名会自动作为别名保留。`,
                confirmText: '确认合并',
                success: (cf) => {
                  if (!cf.confirm) {
                    return;
                  }
                  void mergeProduct(row.id, target.id).then(
                    () => {
                      wx.showToast({ title: `已合并入 ${target.name}`, icon: 'success' });
                      void this.load();
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
        }
      },
    });
  },
});
