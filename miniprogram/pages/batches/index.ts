/**
 * 批次余量：展示商品名、自动批次标签（日期+供应商）与「未到货」虚拟批。只读。
 */

import { ApiError, listBatches, type StockBatchRow } from '../../services/api';
import { ensureLogin } from '../../services/auth';

interface BatchView extends StockBatchRow {
  titleText: string;
  unitCostText: string;
  statusText: string;
}

interface BatchesData {
  loading: boolean;
  items: BatchView[];
  errorText: string;
}

function fenText(fen: number | null, costHidden?: boolean): string {
  if (costHidden) {
    return '成本已隐藏';
  }
  if (fen === null || fen === undefined) {
    return '进价待补';
  }
  return `¥${(fen / 100).toFixed(2).replace(/\.00$/, '')}`;
}

Page<BatchesData, WechatMiniprogram.Page.CustomOption>({
  data: { loading: true, items: [], errorText: '' },

  onShow() {
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const payload = await listBatches();
      const costHidden = Boolean(payload.cost_hidden);
      const items: BatchView[] = (payload.items ?? []).map((row) => {
        const name = row.product_name || row.product_id.slice(0, 8);
        const label = row.label || (row.is_virtual ? '未到货' : '在库批次');
        const statusText = row.is_virtual
          ? '待补进货'
          : row.status === 'open'
            ? '在库'
            : row.status === 'exhausted'
              ? '已用完'
              : '已调整';
        return {
          ...row,
          titleText: `${name} · ${label}`,
          unitCostText: fenText(row.unit_cost_fen, costHidden),
          statusText,
        };
      });
      this.setData({ items, loading: false });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },
});
