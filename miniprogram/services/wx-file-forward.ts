/**
 * 导出文件 → 微信（**不做网页下载**）。
 *
 * 三条硬约束（PRD 3.3 / 架构 §8.4）
 * -------------------------------
 * 1. **不提供网页下载**：导出走 `export_job` 异步生成，端上拿到短 TTL 签名 URL 后
 *    自己转成微信可用的形式；
 * 2. 交付方式三选一：**转发到微信文件传输助手** / **保存到微信** / **微信内预览**；
 * 3. 导出内容**不含音频、不含跨租户**（服务端保证），端上不再二次拼装。
 *
 * ⚠️ 平台能力标注（**需核实**）：
 * - `wx.shareFileMessage` 在部分版本/平台不可用（电脑版微信差异较大）；
 * - `wx.openDocument` 预览 xlsx/csv 的能力随基础库变化；
 * - 不可用时**降级为「复制下载口令 + 提示在微信里打开」**，绝不打开浏览器下载页
 *   （那等于偷偷做了网页版）。
 */

import { listExports, type ExportJobRow } from './api';

/** 交付方式。 */
export type DeliverMode = 'forward' | 'save' | 'preview' | 'unsupported';

/** 一次导出交付的结果。 */
export interface DeliverResult {
  mode: DeliverMode;
  message: string;
}

type FileApi = {
  shareFileMessage?: (params: Record<string, unknown>) => void;
  saveFileToDisk?: (params: Record<string, unknown>) => void;
  openDocument?: (params: Record<string, unknown>) => void;
  downloadFile?: (params: Record<string, unknown>) => void;
};

function apiOf(): FileApi {
  return wx as unknown as FileApi;
}

/** 当前环境支持哪些交付方式（用于 UI 上决定显示哪几个按钮）。 */
export function supportedModes(): DeliverMode[] {
  const api = apiOf();
  const modes: DeliverMode[] = [];
  if (typeof api.shareFileMessage === 'function') {
    modes.push('forward');
  }
  if (typeof api.saveFileToDisk === 'function') {
    modes.push('save');
  }
  if (typeof api.openDocument === 'function') {
    modes.push('preview');
  }
  return modes;
}

/**
 * 把导出任务的结果交付给用户。
 *
 * @param jobId 导出任务 id（服务端异步生成）
 * @param mode 交付方式
 * @returns 实际使用的交付方式与提示文案
 */
export async function deliverExport(jobId: string, mode: DeliverMode): Promise<DeliverResult> {
  // 服务端**没有**「按 job_id 单查」的接口（`GET /api/v1/exports` 只列最近 N 条），
  // 所以这里在列表里按 id 找。找不到就是任务过期/不属于本租户，如实说，不猜。
  const listed = await listExports();
  const job: ExportJobRow | undefined = (listed.items ?? []).find(
    (item) => item.job_id === jobId
  );
  if (job === undefined) {
    return { mode: 'unsupported', message: '找不到这个导出任务了，请回到「导出」重新生成' };
  }
  if (job.status !== 'done') {
    return { mode: 'unsupported', message: '还在生成，稍后到这里取' };
  }
  if (job.signed_url === null || job.signed_url === '') {
    return { mode: 'unsupported', message: '生成失败，请重试' };
  }

  const api = apiOf();
  const localPath = await downloadToLocal(job.signed_url);

  if (mode === 'forward' && typeof api.shareFileMessage === 'function') {
    api.shareFileMessage({
      filePath: localPath,
      fileName: `导出_${jobId}.xlsx`,
      success: () => undefined,
      fail: () => undefined,
    });
    return { mode: 'forward', message: '已打开转发面板，选「文件传输助手」即可' };
  }
  if (mode === 'save' && typeof api.saveFileToDisk === 'function') {
    api.saveFileToDisk({
      filePath: localPath,
      success: () => undefined,
      fail: () => undefined,
    });
    return { mode: 'save', message: '已保存到微信' };
  }
  if (mode === 'preview' && typeof api.openDocument === 'function') {
    api.openDocument({
      filePath: localPath,
      fileType: 'xlsx',
      showMenu: true,
      success: () => undefined,
      fail: () => undefined,
    });
    return { mode: 'preview', message: '微信内预览中' };
  }

  return {
    mode: 'unsupported',
    message: '当前版本微信不支持文件转发，请到电脑版微信「管理台 → 导出」重试（不提供网页下载）',
  };
}

/**
 * 用签名 URL 把文件取到本地临时目录。
 *
 * 注意：这是**微信客户端取文件**，不是「提供网页下载地址给用户」。
 */
function downloadToLocal(signedUrl: string): Promise<string> {
  const api = apiOf();
  return new Promise<string>((resolve, reject) => {
    if (typeof api.downloadFile !== 'function') {
      reject(new Error('当前基础库不支持下载文件'));
      return;
    }
    api.downloadFile({
      url: signedUrl,
      success: (res: { tempFilePath: string }) => resolve(res.tempFilePath),
      fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '下载失败')),
    });
  });
}
