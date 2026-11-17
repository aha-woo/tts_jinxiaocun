/**
 * 隐私授权（录音前置）：**在调任何录音接口之前，先把隐私协议这道门过了**。
 *
 * 为什么必须单独一个文件
 * ----------------------
 * 微信 2023-10-17 起，隐私相关功能**默认全量启用**（不再看 ``app.json`` 里有没有
 * ``__usePrivacyCheck__``）。规则是两条：
 *
 * 1. **后台没声明**（mp 后台「设置 → 服务内容声明 → 用户隐私保护指引」里没勾
 *    对应隐私类型）→ 对应接口**直接禁用**，调用报
 *    ``A:fail api scope is not declared in the privacy agreement``（errno 112）。
 *    同声传译插件内部要调 ``wx.getRecorderManager``（基础库 ≥ 1.9.94），
 *    所以**必须**声明「访问你的麦克风」，否则真机上点大按钮**一点反应都没有**。
 * 2. **后台声明了、但用户没同意过** → 微信会自动弹官方隐私授权弹窗；
 *    开发者若注册了 ``wx.onNeedPrivacyAuthorization``，则改为由开发者自己弹。
 *
 * 官方文档还写明：**隐私协议的审核与前端代码发布是两条线**——即使代码已发版，
 * 隐私协议没更新/没通过审核，新功能照样不生效。所以「后台配好」这一步不能省。
 *
 * 这里做的事只有一件：**在按下大按钮的那一刻，确保授权已经拿到**。
 * 拿不到就**明确告诉用户去哪儿开**，而不是让按钮「点了没反应」。
 *
 * 官方接口（均为基础库 ≥ 2.32.3，低版本直接放行）：
 * - ``wx.getPrivacySetting``：查「还需不需要用户同意」；
 * - ``wx.requirePrivacyAuthorize``：触发授权流程（官方弹窗 / 自定义弹窗）。
 *
 * 官方文档：https://developers.weixin.qq.com/miniprogram/dev/framework/user-privacy/PrivacyAuthorize.html
 */

/** 隐私授权结果。 */
export type PrivacyOutcome =
  /** 已授权（或压根不需要授权）→ 可以录音 */
  | { granted: true }
  /** 用户拒绝 / 出错 → 提示用户去哪儿开 */
  | { granted: false; reason: string; hint: string };

/** 用户拒绝时的统一提示（去哪儿开、怎么开，一步说清）。 */
export const PRIVACY_DENIED_HINT =
  '需要在「设置 → 服务内容声明 → 用户隐私保护指引」里允许「访问你的麦克风」，才能用语音记账';

/** 基础库是否够新（≥ 2.32.3 才有隐私这套；低版本本来就不拦）。 */
function privacyApiAvailable(): boolean {
  const anyWx = wx as unknown as {
    getPrivacySetting?: unknown;
    requirePrivacyAuthorize?: unknown;
  };
  return typeof anyWx.getPrivacySetting === 'function' && typeof anyWx.requirePrivacyAuthorize === 'function';
}

/**
 * 确保隐私授权已拿到（**只在按下大按钮时调用**，不预取）。
 *
 * 为什么不预先弹
 * --------------
 * 用户刚打开小程序就弹隐私协议，体验很差（还没用它就要同意）。正确的时机是
 * 「他要用语音了」——也就是按下大按钮那一刻。
 *
 * @returns 授权结果；`granted=true` 才允许继续录音
 */
export function ensurePrivacyAuthorized(): Promise<PrivacyOutcome> {
  if (!privacyApiAvailable()) {
    // 低版本基础库没有隐私这套机制，也不拦隐私接口 → 直接放行
    return Promise.resolve({ granted: true });
  }

  const anyWx = wx as unknown as {
    getPrivacySetting: (option: {
      success?: (res: { needAuthorization?: boolean; privacyContractName?: string }) => void;
      fail?: (err: { errMsg?: string }) => void;
    }) => void;
    requirePrivacyAuthorize: (option: {
      success?: () => void;
      fail?: (err: { errMsg?: string }) => void;
    }) => void;
  };

  return new Promise<PrivacyOutcome>((resolve) => {
    anyWx.getPrivacySetting({
      success: (res) => {
        // 后台压根没声明隐私类型时，官方明确说会返回 false（= 不需要授权）。
        // 但那种情况下真正调录音接口会报 errno 112「接口被禁用」——
        // 所以这里只在「明确需要授权」时走授权流程，其余交给真实接口去报错。
        if (res.needAuthorization !== true) {
          resolve({ granted: true });
          return;
        }
        anyWx.requirePrivacyAuthorize({
          success: () => resolve({ granted: true }),
          fail: (err) => {
            resolve({
              granted: false,
              reason: err?.errMsg ?? '用户未同意隐私协议',
              hint: PRIVACY_DENIED_HINT,
            });
          },
        });
      },
      fail: () => {
        // 查不到授权状态 ≠ 一定被拒。放行，让真实接口自己判断（失败会有明确 errMsg）
        resolve({ granted: true });
      },
    });
  });
}

/**
 * 麦克风系统权限（``scope.record``）是否已授权。
 *
 * 与隐私协议是**两件事**：
 * - 隐私协议 = 「你收集我的语音这件事，我同意了吗」（后台声明 + 用户点同意）；
 * - 系统权限 = 「这个 App 能不能用麦克风」（用户点的系统弹窗）。
 *
 * @returns `true` = 已授权或尚未询问过（首次会由录音动作自动弹系统弹窗）
 */
export function isRecordScopeGranted(): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    wx.getSetting({
      success: (res) => {
        const auth = res.authSetting as Record<string, boolean> | undefined;
        resolve(auth?.['scope.record'] === true);
      },
      fail: () => resolve(false),
    });
  });
}
