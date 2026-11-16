/**
 * 小程序入口（同一 AppID 同时承载「手机端主录入」与「电脑版微信大屏管理页」）。
 *
 * 关键约束（docs/00-决策速览 A1 / 架构 D14/D15）：
 * - **无独立网页端**：电脑版微信里跑的就是同一个小程序，靠 `app.json` 的
 *   `resizable: true` + 大屏双栏布局适配，不加载任何 H5/浏览器页面；
 * - **无账号密码**：身份来自 `wx.login` 的 code -> 服务端换 JWT（T02 落地）；
 * - 后端通过微信云托管 `callContainer` 私有协议调用（免域名/免备案/免证书）。
 */

import { CLOUD_ENV, CLOUD_SERVICE, ensureCloudReady } from './services/api';

export interface IAppOption {
  globalData: {
    /** 云开发环境 id（占位，部署时替换） */
    cloudEnv: string;
    /** 云托管服务名（callContainer 的 X-WX-SERVICE） */
    cloudService: string;
    /** 屏幕形态：手机端 / 电脑版微信大屏（仅用于布局提示，不影响权限） */
    screen: 'phone' | 'wide';
    /** 后端健康状态缓存（首页展示） */
    healthOk: boolean;
  };
  /** 识别是否运行在电脑版微信大屏（宽屏），仅用于双栏/单栏布局切换 */
  detectScreen(): void;
}

App<IAppOption>({
  globalData: {
    cloudEnv: CLOUD_ENV,
    cloudService: CLOUD_SERVICE,
    screen: 'phone',
    healthOk: false,
  },

  onLaunch() {
    this.detectScreen();
    // 云能力必须在使用 callContainer 之前初始化；失败不阻塞启动（首页会提示）
    ensureCloudReady().catch((error: unknown) => {
      console.error('[app] 云托管初始化失败', error);
    });
  },

  onShow() {
    this.detectScreen();
  },

  /**
   * 识别是否运行在电脑版微信大屏（宽屏）。
   *
   * 仅用于「双栏/单栏」布局切换：电脑版微信没有独立网页容器，
   * 屏幕尺寸由 wx.getWindowInfo 读取（基础库 >= 2.20）。
   */
  detectScreen() {
    try {
      const info = wx.getWindowInfo();
      this.globalData.screen = info.windowWidth >= 768 ? 'wide' : 'phone';
    } catch (error) {
      // 低版本基础库降级：按手机端布局，不影响功能
      this.globalData.screen = 'phone';
      console.warn('[app] getWindowInfo 不可用，按手机端布局', error);
    }
  },
});
