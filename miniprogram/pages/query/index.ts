/**
 * 查询页（R0 只读）：库存 / 欠款 / 流水。
 *
 * 三条铁律：**只读不建卡、不写库、不扣次**；用一个 keyword 输入框 + 三个 tab。
 * 返回结构是 `{kind, items}`，items 的具体字段随 kind 变化，这里做通用渲染
 * （键名直接展示，避免端上臆造业务字段）。
 */

import { ApiError, queryDebt, queryDocs, queryStock } from '../../services/api';

type QueryKind = 'stock' | 'debt' | 'docs';

interface Row {
  text: string;
}

interface QueryData {
  kind: QueryKind;
  keyword: string;
  loading: boolean;
  title: string;
  rows: Row[];
  errorText: string;
}

/** 把任意结果对象拍平成一行文本（只展示，不解析业务语义）。 */
export function flattenRow(item: unknown): Row {
  if (item === null || typeof item !== 'object') {
    return { text: String(item) };
  }
  const entries = Object.entries(item as Record<string, unknown>).filter(
    ([, value]) => value !== null && value !== undefined && value !== ''
  );
  const text = entries.map(([key, value]) => `${key}: ${String(value)}`).join(' · ');
  return { text: text === '' ? '（空）' : text };
}

const TITLES: Record<QueryKind, string> = {
  stock: '库存',
  debt: '欠款',
  docs: '流水',
};

Page<QueryData, WechatMiniprogram.Page.CustomOption>({
  data: { kind: 'stock', keyword: '', loading: false, title: TITLES.stock, rows: [], errorText: '' },

  onLoad(query: Record<string, string | undefined>) {
    const kind = String(query['kind'] ?? 'stock') as QueryKind;
    this.setData({ kind, title: TITLES[kind] ?? TITLES.stock });
    void this.reload();
  },

  onTab(event: WechatMiniprogram.TouchEvent): void {
    const kind = String(event.currentTarget.dataset['kind'] ?? 'stock') as QueryKind;
    this.setData({ kind, title: TITLES[kind] ?? TITLES.stock }, () => void this.reload());
  },

  onKeyword(event: WechatMiniprogram.Input): void {
    this.setData({ keyword: event.detail.value });
  },

  async reload(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      const keyword = this.data.keyword;
      const payload =
        this.data.kind === 'stock'
          ? await queryStock(keyword)
          : this.data.kind === 'debt'
            ? await queryDebt(keyword)
            : await queryDocs(keyword);
      this.setData({
        rows: (payload.items ?? []).map((item) => flattenRow(item)),
        loading: false,
      });
    } catch (error) {
      const message =
        error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message, rows: [] });
    }
  },
});
