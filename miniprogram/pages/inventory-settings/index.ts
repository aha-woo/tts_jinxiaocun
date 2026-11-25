/**
 * 库存设置：成本法（移动平均 / 批次实际成本）、负库存开关与店员成本可见性。
 * 只有老板能改；改动只影响之后的出库。
 */

import {
  ApiError,
  getInventoryPolicy,
  patchInventoryPolicy,
  type InventoryPolicy,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';

interface SettingsData {
  loading: boolean;
  policy: InventoryPolicy;
  isBatchMode: boolean;
  saving: boolean;
  errorText: string;
  notice: string;
}

function isBatchMethod(method: string): boolean {
  return method === 'batch' || method === 'fifo' || method === 'fefe' || method === 'fefo';
}

Page<SettingsData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    policy: { cost_method: 'avg', allow_negative: true, staff_can_view_cost: false },
    isBatchMode: false,
    saving: false,
    errorText: '',
    notice: '',
  },

  onShow() {
    void this.load();
  },

  async load(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      await ensureLogin();
      const policy = await getInventoryPolicy();
      this.setData({
        policy,
        isBatchMode: isBatchMethod(policy.cost_method),
        loading: false,
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ loading: false, errorText: message });
    }
  },

  onMethod(event: WechatMiniprogram.TouchEvent): void {
    const costMethod = String(event.currentTarget.dataset['method'] ?? 'avg') as 'avg' | 'batch';
    this.setData({
      policy: { ...this.data.policy, cost_method: costMethod },
      isBatchMode: isBatchMethod(costMethod),
    });
  },

  onAllowNegative(event: WechatMiniprogram.SwitchChange): void {
    this.setData({ policy: { ...this.data.policy, allow_negative: Boolean(event.detail.value) } });
  },

  onStaffViewCost(event: WechatMiniprogram.SwitchChange): void {
    this.setData({
      policy: { ...this.data.policy, staff_can_view_cost: Boolean(event.detail.value) },
    });
  },

  async onSave(): Promise<void> {
    this.setData({ saving: true, errorText: '', notice: '' });
    try {
      const policy = await patchInventoryPolicy({
        cost_method: this.data.policy.cost_method,
        allow_negative: this.data.policy.allow_negative,
        staff_can_view_cost: Boolean(this.data.policy.staff_can_view_cost),
      });
      this.setData({
        policy,
        isBatchMode: isBatchMethod(policy.cost_method),
        saving: false,
        notice: '已保存；成本法只影响之后的出库，不重算历史',
      });
    } catch (error) {
      const message = error instanceof ApiError ? `${error.message}（${error.errorCode}）` : String(error);
      this.setData({ saving: false, errorText: message });
    }
  },

  onBatches(): void {
    wx.navigateTo({ url: '/pages/batches/index' });
  },
});
