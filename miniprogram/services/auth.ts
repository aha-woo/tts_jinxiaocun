/**
 * 登录态守卫。
 *
 * 问题：登录只在首页 bootstrap 里做，直接切到货品/欠款会先发请求，后端回「缺少登录凭证」。
 * 做法：所有需要鉴权的页面在请求前 `await ensureLogin()`，同一时刻只发一次 wx.login。
 */

import { getToken, login, registerTokenClearHook } from './api';

let pending: Promise<void> | null = null;
let loggedIn = false;

export function resetLoginState(): void {
  loggedIn = false;
}

registerTokenClearHook(resetLoginState);

function doLogin(): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    wx.login({
      success: (res: { code: string }) => {
        login(res.code)
          .then(() => resolve())
          .catch((error: unknown) => reject(error));
      },
      fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '登录失败')),
    });
  });
}

/** 已有 token 直接返回；否则并发安全地登录一次。 */
export function ensureLogin(): Promise<void> {
  if (loggedIn || getToken() !== '') {
    loggedIn = true;
    return Promise.resolve();
  }
  if (pending !== null) {
    return pending;
  }
  pending = doLogin().then(
    () => {
      loggedIn = true;
      pending = null;
    },
    (error: unknown) => {
      pending = null;
      throw error;
    }
  );
  return pending;
}
