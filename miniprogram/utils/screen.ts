/**
 * 屏幕宽度断点与电脑版微信大屏判定。
 * 手机端 < 768px；电脑版微信大屏（`resizable: true`）>= 768px。
 */

export const WIDE_BREAKPOINT = 768;

/** 读取当前窗口是否处于宽屏模式（异常时保守按窄屏处理）。 */
export function isWideScreen(): boolean {
  try {
    const info = wx.getWindowInfo();
    return info.windowWidth >= WIDE_BREAKPOINT;
  } catch (error) {
    console.warn('[screen] getWindowInfo 不可用，按窄屏处理', error);
    return false;
  }
}
