/**
 * 首页（T01 占位页）：显示后端健康状态 + 契约计数 + 「初始化云托管」引导。
 *
 * T05 会把它替换为语音大按钮主界面；此处保留为联调自检页，
 * 端到端验收时用它确认 `callContainer` 链路与端服契约同版本。
 */

import type { IAppOption } from '../../app';
import {
  ApiError,
  CLOUD_ENV,
  CLOUD_SERVICE,
  ensureCloudReady,
  health,
  HealthPayload,
} from '../../services/api';
import { formatDateTime } from '../../utils/format';

interface IndexData {
  loading: boolean;
  reachable: boolean;
  statusText: string;
  statusClass: string;
  version: string;
  checkedAt: string;
  backend: string;
  counts: {
    intents: number;
    slots: number;
    sources: number;
    errorCodes: number;
    domains: number;
  };
  cloudEnv: string;
  cloudService: string;
  envConfigured: boolean;
  errorText: string;
  lastErrorCode: string;
}

Page<IndexData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: false,
    reachable: false,
    statusText: '还没检查',
    statusClass: 'muted',
    version: '-',
    checkedAt: '-',
    backend: '-',
    counts: {
      intents: 0,
      slots: 0,
      sources: 0,
      errorCodes: 0,
      domains: 0,
    },
    cloudEnv: CLOUD_ENV,
    cloudService: CLOUD_SERVICE,
    envConfigured: CLOUD_ENV !== 'REPLACE_WITH_CLOUD_ENV_ID',
    errorText: '',
    lastErrorCode: '',
  },

  onLoad() {
    void this.checkHealth();
  },

  onShow() {
    // 从大屏管理页返回时刷新一次，保证两端状态一致
    if (this.data.reachable) {
      void this.checkHealth();
    }
  },

  /** 按钮「重新检查后端」：包一层 loading 态。 */
  onCheckHealthTap() {
    void this.checkHealth();
  },

  /** 手动检查后端健康状态。 */
  async checkHealth() {
    this.setData({ loading: true, errorText: '', lastErrorCode: '' });
    try {
      const payload: HealthPayload = await health();
      this.setData({
        loading: false,
        reachable: true,
        statusText: payload.status === 'ok' ? '后端正常' : '后端降级中',
        statusClass: payload.status === 'ok' ? 'text-ok' : 'text-warn',
        version: payload.version,
        checkedAt: formatDateTime(payload.checked_at),
        backend: payload.idempotency.backend,
        counts: {
          intents: payload.contract.intents,
          slots: payload.contract.slots,
          sources: payload.contract.sources,
          errorCodes: payload.contract.error_codes,
          domains: payload.contract.domains,
        },
      });
      const app = getApp<IAppOption>();
      app.globalData.healthOk = true;
    } catch (error) {
      const apiError = error instanceof ApiError ? error : null;
      this.setData({
        loading: false,
        reachable: false,
        statusText: '连不上后端',
        statusClass: 'text-danger',
        errorText: apiError !== null ? apiError.message : String(error),
        lastErrorCode: apiError !== null ? apiError.errorCode : '',
      });
      const app = getApp<IAppOption>();
      app.globalData.healthOk = false;
    }
  },

  /** 「初始化云托管」：wx.cloud.init + 重新探活。 */
  async onInitCloudTap() {
    wx.showLoading({ title: '初始化云托管' });
    try {
      await ensureCloudReady();
      wx.hideLoading();
      wx.showToast({ title: '云托管已就绪', icon: 'success' });
      await this.checkHealth();
    } catch (error) {
      wx.hideLoading();
      wx.showModal({
        title: '云托管初始化失败',
        content:
          `${String(error)}\n\n请确认：\n1) services/api.ts 的 CLOUD_ENV 已替换为真实云开发环境 id；\n` +
          '2) 云托管服务已部署且服务名与 CLOUD_SERVICE 一致；\n' +
          '3) 基础库 >= 2.23.0（callContainer 要求）。',
        showCancel: false,
      });
    }
  },

  /** 复制环境 id，方便部署时核对（不涉及任何密钥）。 */
  onCopyEnvTap() {
    wx.setClipboardData({
      data: `${CLOUD_ENV} / ${CLOUD_SERVICE}`,
      success: () => {
        wx.showToast({ title: '已复制', icon: 'none' });
      },
    });
  },
});
