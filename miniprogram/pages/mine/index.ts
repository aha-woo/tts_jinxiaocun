/**
 * 我的（真实账号数据 + 记账方式/确认方式偏好 + 字号持久化 + 说法本管理）。
 */

import {
  ApiError,
  deleteDict,
  getMe,
  listDict,
  queryReport,
  teachDict,
  updatePreferences,
  type DictRow,
  type MeMemberRow,
  type NoviceStatus,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';

const ROLE_LABELS: Record<string, string> = {
  boss: '老板',
  manager: '店长',
  clerk: '店员',
  readonly: '只读',
};

interface MineData {
  displayName: string;
  storeName: string;
  roleLabel: string;
  mode: 'each' | 'count';
  confirmMode: 'always' | 'auto';
  novice: NoviceStatus | null;
  fontSize: 'big' | 'std';
  aliasCount: number;
  monthlySell: string;
  monthlyProfit: string;
  dictOpen: boolean;
  dictItems: DictRow[];
  members: MeMemberRow[];
  errorText: string;
}

Page<MineData, WechatMiniprogram.Page.CustomOption>({
  data: {
    displayName: '店主',
    storeName: '本店',
    roleLabel: '老板',
    mode: 'each',
    confirmMode: 'always',
    novice: null,
    fontSize: 'big',
    aliasCount: 0,
    monthlySell: '—',
    monthlyProfit: '—',
    dictOpen: false,
    dictItems: [],
    members: [],
    errorText: '',
  },

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 3 });
    const savedSize = (wx.getStorageSync('dsh:fontSize') as string) === 'std' ? 'std' : 'big';
    this.setData({ fontSize: savedSize });
    void this.load();
  },

  async load(): Promise<void> {
    try {
      await ensureLogin();
      const [me, rep, dict] = await Promise.all([getMe(), queryReport('today'), listDict()]);
      const report = (rep.report ?? {}) as Record<string, unknown>;
      const sellFen = Number(report['sales_fen'] ?? 0);
      const profitFen = report['gross_profit_fen'];
      const yuan = (fen: unknown): string => {
        if (fen === null || fen === undefined) {
          return '—';
        }
        const n = Number(fen);
        return Number.isFinite(n) ? `¥${(n / 100).toFixed(2).replace(/\.00$/, '')}` : '—';
      };
      this.setData({
        displayName: me.display_name || '店主',
        storeName: me.store_name || '本店',
        roleLabel: ROLE_LABELS[me.role] ?? me.role,
        mode: me.accounting_mode === 'count_derive' ? 'count' : 'each',
        confirmMode: me.auto_post_enabled ? 'auto' : 'always',
        novice: me.novice,
        aliasCount: me.alias_count,
        monthlySell: yuan(sellFen),
        monthlyProfit: me.can_view_cost ? yuan(profitFen) : '已隐藏',
        dictItems: dict.items ?? [],
        members: me.members ?? [],
        errorText: '',
      });
    } catch (error) {
      const msg = error instanceof ApiError ? error.message : String(error);
      this.setData({ errorText: msg });
    }
  },

  async onMode(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const mode = String(event.currentTarget.dataset['mode'] ?? 'each') as 'each' | 'count';
    const accounting_mode = mode === 'count' ? 'count_derive' : 'per_txn';
    try {
      await updatePreferences({ accounting_mode });
      this.setData({ mode });
      wx.showToast({ title: mode === 'each' ? '已选：逐笔记' : '已选：进货 + 盘点倒推', icon: 'none' });
    } catch (error) {
      wx.showToast({ title: error instanceof ApiError ? error.message : '修改失败', icon: 'none' });
    }
  },

  async onConfirmMode(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const target = String(event.currentTarget.dataset['confirm'] ?? 'always') as 'always' | 'auto';
    if (target === 'auto' && this.data.novice?.in_novice) {
      wx.showToast({
        title: `新手期 (${this.data.novice.count}/${this.data.novice.target}) 内每笔都需确认`,
        icon: 'none',
      });
      return;
    }
    try {
      await updatePreferences({ auto_post_enabled: target === 'auto' });
      this.setData({ confirmMode: target });
      wx.showToast({
        title: target === 'auto' ? '已开启：有把握直接记（5秒内可撤）' : '已切换：每笔都出确认卡',
        icon: 'none',
      });
    } catch (error) {
      wx.showToast({ title: error instanceof ApiError ? error.message : '修改失败', icon: 'none' });
    }
  },

  onFontSize(event: WechatMiniprogram.TouchEvent): void {
    const fontSize = String(event.currentTarget.dataset['size'] ?? 'big') as 'big' | 'std';
    wx.setStorageSync('dsh:fontSize', fontSize);
    this.setData({ fontSize });
    wx.showToast({ title: fontSize === 'big' ? '已切到大字号' : '已切到标准字号', icon: 'none' });
  },

  onToggleDict(): void {
    this.setData({ dictOpen: !this.data.dictOpen });
  },

  onAddDict(): void {
    wx.showModal({
      title: '教一个新说法',
      editable: true,
      placeholderText: '例如：快乐水',
      success: (res) => {
        const raw = String(res.content ?? '').trim();
        if (res.confirm && raw) {
          void teachDict({ raw_text: raw, mapped_type: 'product' }).then(() => {
            wx.showToast({ title: '已记住', icon: 'success' });
            void this.load();
          });
        }
      },
    });
  },

  onDeleteDict(event: WechatMiniprogram.TouchEvent): void {
    const dictId = String(event.currentTarget.dataset['id'] ?? '');
    if (!dictId) {
      return;
    }
    void deleteDict(dictId).then(() => {
      wx.showToast({ title: '已删', icon: 'none' });
      void this.load();
    });
  },

  onShowMembers(): void {
    const lines = this.data.members.map((m) => {
      const role = ROLE_LABELS[m.role] ?? m.role;
      const st = m.status === 'active' ? '正常' : '已停用';
      return `${m.name}（${role} · ${st}${m.is_self ? ' · 当前账号' : ''}）`;
    });
    wx.showModal({
      title: '店里的人',
      content: lines.length > 0 ? lines.join('\n') : '暂无其他成员记录',
      showCancel: false,
    });
  },

  onGoPay(): void {
    wx.navigateTo({ url: '/pages/pay/index' });
  },

  onGoAdmin(): void {
    wx.navigateTo({ url: '/pages/admin/index' });
  },

  onGoPending(): void {
    wx.navigateTo({ url: '/pages/pending/index' });
  },

  onGoInventory(): void {
    wx.navigateTo({ url: '/pages/inventory-settings/index' });
  },

  onTip(event: WechatMiniprogram.TouchEvent): void {
    const tip = String(event.currentTarget.dataset['tip'] ?? '');
    wx.showModal({ title: '说明', content: tip, showCancel: false });
  },
});
