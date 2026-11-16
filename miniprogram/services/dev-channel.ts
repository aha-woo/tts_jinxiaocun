/**
 * **仅供本地联调**的 dev 通道（生产构建里 `DEV_MODE` 必须是 `false`）。
 *
 * 为什么单独一个文件
 * -----------------
 * 「生产只走微信云托管 `callContainer`」是硬约束（免域名/免备案/免证书）。
 * 本机没有云托管环境，集成联调需要一条 `http://127.0.0.1:8000` 直连；
 * 为了能**机械检查**「生产路径里不存在 http 直连分支」，所有 `http://` 与
 * `wx.request` 只准出现在本文件里：
 *
 * - `services/api.ts`（唯一生产出网路径）**不得**出现 `http://` 或 `wx.request`；
 * - 本文件的 `DEV_MODE` 默认 `false`，为 `true` 时 api.ts 才会调 {@link devCall}；
 * - `server/tests/test_miniprogram_prod_channel.py` 断言上面两条，防有人偷偷打开。
 *
 * ⚠️ 上线前复查：`DEV_MODE` 必须为 `false`，且 `project.config.json` 的
 * `setting.urlCheck` 保持 `true`（生产态不允许任意域名请求）。
 */

/** 本地联调开关。**生产必须为 `false`**。 */
export const DEV_MODE = false;

/** 本地联调地址（只在 `DEV_MODE === true` 时被使用）。 */
export const DEV_BASE_URL = 'http://127.0.0.1:8000';

/** dev 通道请求参数（与 api.ts 的 RequestOptions 结构一致，避免循环依赖）。 */
export interface DevRequest {
  path: string;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  header: Record<string, string>;
  data: Record<string, unknown>;
  timeout: number;
}

/** dev 通道响应（只要状态码与已解析的 JSON）。 */
export interface DevResponse {
  statusCode: number;
  data: unknown;
}

/**
 * 走 `wx.request` 直连本地 uvicorn（**仅 dev**）。
 *
 * @param request 路径/方法/头/体/超时
 * @returns 状态码 + 已解包的响应体
 * @throws Error 网络失败（由调用方包装成 ApiError）
 */
export function devCall(request: DevRequest): Promise<DevResponse> {
  return new Promise<DevResponse>((resolve, reject) => {
    wx.request({
      url: `${DEV_BASE_URL}${request.path}`,
      // 微信 wx.request 的类型声明里没有 PATCH（平台限制），dev 通道降级成 POST
      method: request.method === 'PATCH' ? 'POST' : request.method,
      header: request.header,
      data: request.data,
      timeout: request.timeout,
      success: (res: { statusCode: number; data: unknown }) => {
        resolve({ statusCode: res.statusCode, data: res.data });
      },
      fail: (error: { errMsg?: string }) => {
        reject(new Error(`dev 通道网络异常：${error.errMsg ?? '未知错误'}`));
      },
    });
  });
}
