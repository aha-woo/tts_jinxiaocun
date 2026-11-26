/**
 * 我的（真实账号数据 + 记账方式/确认方式偏好 + 字号持久化 + 说法本管理）。
 */

import {
  ApiError,
  createStaffInvite,
  deleteDict,
  getMe,
  joinStaffByInvite,
  listDict,
  queryReport,
  sendClosingSummary,
  setToken,
  teachDict,
  updatePreferences,
  updateStaffStatus,
  type DictRow,
  type MeMemberRow,
  type NoviceStatus,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
import { isWideScreen } from '../../utils/screen';

const ROLE_LABELS: Record<string, string> = {
  boss: '老板',
  manager: '店长',
  clerk: '店员',
  readonly: '只读',
};

interface MineData {
  wide: boolean;
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
  membersOpen: boolean;
  isBoss: boolean;
  members: MeMemberRow[];
  errorText: string;
}

Page<MineData, WechatMiniprogram.Page.CustomOption>({
  data: {
    wide: false,
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
    membersOpen: false,
    isBoss: true,
    members: [],
    errorText: '',
  },

  onShow() {
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 3 });
    const savedSize = (wx.getStorageSync('dsh:fontSize') as string) === 'std' ? 'std' : 'big';
    this.setData({ wide: isWideScreen(), fontSize: savedSize });
    void this.load();
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
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
        isBoss: me.role === 'boss',
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

  onToggleMembers(): void {
    this.setData({ membersOpen: !this.data.membersOpen });
  },

  onCreateInvite(): void {
    void createStaffInvite('clerk').then(
      (res) => {
        wx.showModal({
          title: '店员邀请码（24 小时有效）',
          content: `让店员在「我的 → 店里的人 → 输入邀请码入店」粘贴这串码：\n\n${res.invite_code}`,
          confirmText: '复制邀请码',
          cancelText: '关掉',
          success: (m) => {
            if (m.confirm) {
              wx.setClipboardData({ data: res.invite_code });
            }
          },
        });
      },
      (err: unknown) => {
        wx.showToast({ title: err instanceof ApiError ? err.message : '生成失败', icon: 'none' });
      }
    );
  },

  onJoinByInvite(): void {
    wx.showModal({
      title: '加入老板的店铺',
      editable: true,
      placeholderText: '粘贴 DSH1. 开头的邀请码',
      success: (res) => {
        const code = String(res.content ?? '').trim();
        if (!res.confirm || !code) {
          return;
        }
        void joinStaffByInvite(code).then(
          (joined) => {
            setToken(joined.jwt);
            wx.showModal({
              title: `已加入「${joined.store_name}」`,
              content: `你的角色已切换为「${ROLE_LABELS[joined.role] ?? joined.role}」，下次说话即可协同记账。`,
              showCancel: false,
              success: () => {
                void this.load();
              },
            });
          },
          (err: unknown) => {
            wx.showModal({
              title: '没加入成功',
              content: err instanceof ApiError ? err.message : String(err),
              showCancel: false,
            });
          }
        );
      },
    });
  },

  onToggleMemberStatus(event: WechatMiniprogram.TouchEvent): void {
    const memberId = String(event.currentTarget.dataset['id'] ?? '');
    const current = String(event.currentTarget.dataset['status'] ?? 'active');
    const nextStatus = current === 'active' ? 'disabled' : 'active';
    if (!memberId) {
      return;
    }
    wx.showModal({
      title: nextStatus === 'disabled' ? '确认停用店员' : '确认恢复店员',
      content:
        nextStatus === 'disabled'
          ? '停用后对方下次操作立即失效（已记的单据仍保留审计痕迹）。'
          : '恢复后对方可正常继续记账。',
      success: (res) => {
        if (!res.confirm) {
          return;
        }
        void updateStaffStatus(memberId, { status: nextStatus }).then(
          () => {
            wx.showToast({ title: nextStatus === 'disabled' ? '已停用' : '已启用', icon: 'success' });
            void this.load();
          },
          (err: unknown) => {
            wx.showToast({ title: err instanceof ApiError ? err.message : '操作失败', icon: 'none' });
          }
        );
      },
    });
  },

  onSendClosingSummary(): void {
    void sendClosingSummary().then(
      (res) => {
        const s = res.summary;
        const extra = res.sent
          ? '（已推送到微信订阅消息）'
          : '（订阅消息模板未配置或未授权，展示今日收摊简报）';
        wx.showModal({
          title: `今日收摊总结${extra}`,
          content: `${s.summary_text}\n三账核对：${s.reconcile_ok ? '账实相符' : '发现差异'}\n负库存商品数：${s.negative_count}`,
          showCancel: false,
        });
      },
      (err: unknown) => {
        wx.showToast({ title: err instanceof ApiError ? err.message : '生成失败', icon: 'none' });
      }
    );
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
