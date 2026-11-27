/**
 * 手动记账（**全程脱离语音**）：选方向 → 搜/选商品 → 填数量 → 填单价 → 生成卡片。
 *
 * 常用货判定（诚实、可解释）：
 * 1. 本机最近手动记过的货（`vi.manual.recent`，最多 6 样）；
 * 2. 近 7 天真实动销排行（`GET /query/report?period=top`）；
 * 3. 新店既无手记也无热销时，才按全店货品前 6 样兜底并标注「店内商品」；
 * 4. 全店所有货品随时可通过关键词搜索联想或点「全部货品」抽屉挑选。
 */

import { ApiError, createVoiceSession, parseSession, queryReport, queryStock } from '../../services/api';

const RECENT_STORAGE_KEY = 'vi.manual.recent';
const MAX_QUICK_ITEMS = 6;
const DEFAULT_UNITS = ['件', '桶', '瓶', '箱', '罐', '袋', '盒', '斤', '条', '包'];

export interface CatalogItem {
  name: string;
  stockText: string;
  unit: string;
}

/** 把点选结果拼成一句规范话（与口语同构，便于规则层复用）。 */
export function composeUtterance(input: {
  direction: 'in' | 'out';
  product: string;
  quantity: string;
  unit?: string;
  price: string;
}): string {
  const verb = input.direction === 'in' ? '进货' : '卖货';
  const parts = [verb, input.product];
  if (input.quantity !== '') {
    parts.push(`${input.quantity}${input.unit || '件'}`);
  }
  if (input.price !== '') {
    parts.push(`单价${input.price}元`);
  }
  return parts.join('，');
}

function readRecentProducts(): string[] {
  try {
    const raw = wx.getStorageSync(RECENT_STORAGE_KEY);
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw.map((x) => String(x ?? '').trim()).filter((x) => x !== '').slice(0, MAX_QUICK_ITEMS);
  } catch {
    return [];
  }
}

function rememberRecentProduct(name: string): void {
  const clean = name.trim();
  if (clean === '') {
    return;
  }
  try {
    const prev = readRecentProducts().filter((item) => item !== clean);
    wx.setStorageSync(RECENT_STORAGE_KEY, [clean, ...prev].slice(0, MAX_QUICK_ITEMS));
  } catch {
    // 忽略本地存储异常
  }
}

interface ManualData {
  product: string;
  customProduct: string;
  quantity: string;
  unit: string;
  unitOptions: string[];
  price: string;
  direction: 'in' | 'out';
  catalog: CatalogItem[];
  quickProducts: string[];
  quickLabel: string;
  suggestions: CatalogItem[];
  pickerOpen: boolean;
  pickerKeyword: string;
  pickerFiltered: CatalogItem[];
  submitting: boolean;
  errorText: string;
  preview: string;
}

Page<ManualData, WechatMiniprogram.Page.CustomOption>({
  data: {
    product: '',
    customProduct: '',
    quantity: '',
    unit: '件',
    unitOptions: DEFAULT_UNITS,
    price: '',
    direction: 'in',
    catalog: [],
    quickProducts: [],
    quickLabel: '常用货',
    suggestions: [],
    pickerOpen: false,
    pickerKeyword: '',
    pickerFiltered: [],
    submitting: false,
    errorText: '',
    preview: '',
  },

  onLoad(query: Record<string, string | undefined>) {
    const direction = query['direction'] === 'out' ? 'out' : 'in';
    const quantity = String(query['qty'] ?? '');
    const price = String(query['price'] ?? '');
    this.setData({
      direction,
      quantity,
      price,
      preview: composeUtterance({ direction, product: '', quantity, unit: '件', price }),
    });
    void this.loadCatalog();
  },

  async loadCatalog(): Promise<void> {
    const recent = readRecentProducts();
    const [stockItems, topProducts] = await Promise.all([
      queryStock('', false, 100).then((res) => res.items ?? []).catch(() => [] as unknown[]),
      queryReport('top')
        .then((res) => ((res as { top_products?: unknown[] }).top_products ?? []))
        .catch(() => [] as unknown[]),
    ]);

    const catalog: CatalogItem[] = [];
    const seenCatalog = new Set<string>();
    for (const raw of stockItems) {
      const o = raw as Record<string, unknown>;
      const name = String(o['product_name'] ?? o['name'] ?? '').trim();
      if (name === '' || seenCatalog.has(name)) {
        continue;
      }
      seenCatalog.add(name);
      const unit = String(o['base_unit'] ?? o['unit'] ?? '');
      const qty = o['qty_display'] ?? o['qty_base'] ?? '';
      const stockText = qty !== '' ? `${qty}${unit}` : '';
      catalog.push({ name, stockText, unit });
    }

    const topNames: string[] = [];
    for (const raw of topProducts) {
      const name = String((raw as Record<string, unknown>)['product_name'] ?? '').trim();
      if (name !== '') {
        topNames.push(name);
      }
    }

    const quick: string[] = [];
    const seenQuick = new Set<string>();
    for (const name of [...recent, ...topNames]) {
      if (!seenQuick.has(name)) {
        seenQuick.add(name);
        quick.push(name);
      }
      if (quick.length >= MAX_QUICK_ITEMS) {
        break;
      }
    }

    let quickLabel = '近 7 天热销';
    if (recent.length > 0 && topNames.length > 0) {
      quickLabel = '最近常记 / 近 7 天热销';
    } else if (recent.length > 0) {
      quickLabel = '最近手记';
    } else if (quick.length === 0 && catalog.length > 0) {
      quickLabel = '店内商品';
      for (const item of catalog.slice(0, MAX_QUICK_ITEMS)) {
        quick.push(item.name);
      }
    }

    this.setData({
      catalog,
      quickProducts: quick,
      quickLabel,
      pickerFiltered: catalog,
    });
  },

  onPickDirectionTab(event: WechatMiniprogram.TouchEvent): void {
    const dir = String(event.currentTarget.dataset['direction'] ?? 'in') === 'out' ? 'out' : 'in';
    this.setData({ direction: dir }, () => this.refreshPreview());
  },

  onPickProductChip(event: WechatMiniprogram.TouchEvent): void {
    const name = String(event.currentTarget.dataset['name'] ?? '').trim();
    if (name === '') {
      return;
    }
    this.selectProductName(name);
  },

  onPickSuggestion(event: WechatMiniprogram.TouchEvent): void {
    const name = String(event.currentTarget.dataset['name'] ?? '').trim();
    if (name === '') {
      return;
    }
    this.selectProductName(name);
  },

  selectProductName(name: string): void {
    const matched = this.data.catalog.find((item) => item.name === name);
    const nextUnit = matched && matched.unit !== '' ? matched.unit : this.data.unit || '件';
    const unitOptions =
      nextUnit !== '' && !DEFAULT_UNITS.includes(nextUnit)
        ? [nextUnit, ...DEFAULT_UNITS]
        : DEFAULT_UNITS;
    this.setData(
      {
        product: name,
        customProduct: name,
        unit: nextUnit,
        unitOptions,
        suggestions: [],
        pickerOpen: false,
      },
      () => this.refreshPreview()
    );
  },

  onCustomProductInput(event: WechatMiniprogram.Input): void {
    const val = String(event.detail.value ?? '').trim();
    const lower = val.toLowerCase();
    const matched = this.data.catalog.find((item) => item.name === val);
    const nextUnit = matched && matched.unit !== '' ? matched.unit : this.data.unit || '件';
    const unitOptions =
      nextUnit !== '' && !DEFAULT_UNITS.includes(nextUnit)
        ? [nextUnit, ...DEFAULT_UNITS]
        : DEFAULT_UNITS;
    const suggestions =
      val === ''
        ? []
        : this.data.catalog
            .filter((item) => item.name.toLowerCase().includes(lower) && item.name !== val)
            .slice(0, 5);
    this.setData(
      { customProduct: val, product: val, unit: nextUnit, unitOptions, suggestions },
      () => this.refreshPreview()
    );
  },

  onPickUnit(event: WechatMiniprogram.TouchEvent): void {
    const unit = String(event.currentTarget.dataset['unit'] ?? '').trim();
    if (unit === '') {
      return;
    }
    this.setData({ unit }, () => this.refreshPreview());
  },

  onClearProduct(): void {
    this.setData({ customProduct: '', product: '', suggestions: [] }, () => this.refreshPreview());
  },

  onOpenPicker(): void {
    this.setData({
      pickerOpen: true,
      pickerKeyword: '',
      pickerFiltered: this.data.catalog,
    });
  },

  onClosePicker(): void {
    this.setData({ pickerOpen: false });
  },

  onPickerSearchInput(event: WechatMiniprogram.Input): void {
    const kw = String(event.detail.value ?? '').trim();
    const lower = kw.toLowerCase();
    const filtered =
      kw === ''
        ? this.data.catalog
        : this.data.catalog.filter((item) => item.name.toLowerCase().includes(lower));
    this.setData({ pickerKeyword: kw, pickerFiltered: filtered });
  },

  noop(): void {},

  onGoPhoto(): void {
    wx.navigateTo({ url: '/pages/delivery-note/index' });
  },

  onProduct(event: WechatMiniprogram.CustomEvent<{ product: string }>): void {
    this.selectProductName(event.detail.product);
  },

  onDirection(event: WechatMiniprogram.CustomEvent<{ direction: 'in' | 'out' }>): void {
    this.setData({ direction: event.detail.direction }, () => this.refreshPreview());
  },

  onInput(event: WechatMiniprogram.CustomEvent<{ field: 'quantity' | 'price'; value: string }>): void {
    const patch: Record<string, string> = {};
    patch[event.detail.field] = event.detail.value;
    this.setData(patch, () => this.refreshPreview());
  },

  refreshPreview(): void {
    this.setData({
      preview: composeUtterance({
        direction: this.data.direction,
        product: this.data.product,
        quantity: this.data.quantity,
        unit: this.data.unit,
        price: this.data.price,
      }),
    });
  },

  /** 提交：建会话（0 音频）→ 用拼好的文本解析 → 有卡片就跳确认页。 */
  async onSubmit(): Promise<void> {
    if (this.data.product === '' || this.data.quantity === '') {
      this.setData({ errorText: '商品和数量要先填上' });
      return;
    }
    this.setData({ submitting: true, errorText: '' });
    try {
      const session = await createVoiceSession({ duration_ms: 0, mode: 'tap', source: 'manual' });
      if (session.session_id === null || session.session_id === '') {
        this.setData({ errorText: session.message_zh || '暂不能创建会话，请稍后再试' });
        return;
      }
      const parsed = await parseSession(session.session_id, {
        asr_text: this.data.preview,
        asr_confidence: 1.0,
        duration_ms: 0,
      });
      if (parsed.route === 'auto_posted' || parsed.auto_posted === true) {
        rememberRecentProduct(this.data.product);
        const docNo = parsed.result?.doc_no ?? '';
        wx.showToast({ title: docNo ? `已直记 ${docNo}` : '已直记', icon: 'success' });
        setTimeout(() => wx.navigateBack({ delta: 1 }), 700);
        return;
      }
      if (parsed.card !== null && parsed.card !== undefined) {
        rememberRecentProduct(this.data.product);
        wx.redirectTo({ url: `/pages/card/index?cardId=${parsed.card.meta.card_id}` });
        return;
      }
      if (parsed.clarify !== null && parsed.clarify !== undefined) {
        rememberRecentProduct(this.data.product);
        wx.setStorageSync('dsh:pendingClarify', {
          sessionId: session.session_id,
          clarify: parsed.clarify,
        });
        wx.redirectTo({ url: `/pages/card/index?sessionId=${session.session_id}` });
        return;
      }
      this.setData({ errorText: `没生成卡片（${parsed.route}）` });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? `${error.message}${error.errorCode ? `（${error.errorCode}）` : ''}`
          : String(error);
      this.setData({ errorText: message });
    } finally {
      this.setData({ submitting: false });
    }
  },
});
