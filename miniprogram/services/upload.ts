/**
 * 音频**直传**对象存储：音频本体**绝不进 `callContainer` 请求体**。
 *
 * 硬约束（架构 §8.4「音频只传引用」）
 * ---------------------------------
 * - `callContainer` 请求体是 **文本 100KiB 硬限制**，音频一进去就超；
 * - 端侧用 `wx.cloud.uploadFile` 直传云开发文件（**不需配服务器合法域名**；
 *   `wx.uploadFile` 才需要配，本设计禁用）；
 * - 服务端只接 `audio_object_ref`（临时 fileID），TTL 到点销毁，音频默认不留存。
 *
 * 流程：向服务端要 `upload_ticket`（短 TTL + `t/{tenant_id}/` 前缀）
 *      → 直传 → 只把 fileID 交给解析接口。
 */

/** 上传票据（由 `POST /api/v1/voice/sessions` 一并下发，短 TTL）。 */
export interface UploadTicket {
  ticket_id: string;
  /** 传给 `/parse` 的引用（服务端已按 `t/{tenant_id}/` 打前缀） */
  audio_object_ref: string;
  /** 直传路径前缀（端上拼 session 唯一名，避免覆盖） */
  upload_prefix: string;
  expires_at: string;
  max_bytes: number;
}

/**
 * 从 `statSync` 的两种返回形状里取文件体积。
 *
 * 基础库传单路径时返回 `Stats`（直接有 `size`），传数组时返回 `FileStats[]`
 * （每项是 `{path, stats}`）。取不到就当 0，服务端仍会按真实体积二次校验。
 */
function readSize(
  stat: WechatMiniprogram.Stats | WechatMiniprogram.FileStats[]
): number {
  const entry = Array.isArray(stat) ? stat[0] : stat;
  if (entry === undefined) {
    return 0;
  }
  const stats = 'stats' in entry ? entry.stats : entry;
  return typeof stats.size === 'number' ? stats.size : 0;
}

/** 直传结果。 */
export interface UploadResult {
  /** 传给 `/voice/parse` 的引用（**不是字节**） */
  audioObjectRef: string;
  sizeBytes: number;
  durationMs: number;
}

/**
 * 把本地临时录音直传到对象存储，返回**引用**。
 *
 * @param tempFilePath `RecorderManager.onStop` 给的临时路径
 * @param durationMs 录音时长（服务端据此判时长上限与计费）
 * @param ticket 会话创建时下发的上传票据（含 `upload_prefix`）
 * @returns 引用 + 体积 + 时长
 * @throws Error 上传失败（调用方提示重试，不扣次）
 */
export async function uploadAudio(
  tempFilePath: string,
  durationMs: number,
  ticket: UploadTicket
): Promise<UploadResult> {
  const fileSize = await new Promise<number>((resolve) => {
    // `wx.getFileInfo` 在新基础库里被 `getFileSystemManager().stat` 取代，
    // 两套 API 都试一遍；都拿不到就按 0 处理（服务端仍会按真实体积校验）。
    const fs = wx.getFileSystemManager();
    try {
      const stat = fs.statSync(tempFilePath);
      // 传 string 路径时返回 `Stats`；传数组时返回 `FileStats[]`（`{path, stats}`）。
      // 这里只有单路径分支，但两种形状都兜住，避免基础库差异导致崩溃。
      resolve(readSize(stat));
    } catch (error) {
      console.warn('[upload] 取文件体积失败，按 0 处理', error);
      resolve(0);
    }
  });

  const uploaded = await new Promise<{ fileID: string }>((resolve, reject) => {
    wx.cloud.uploadFile({
      cloudPath: `${ticket.upload_prefix}${Date.now()}.mp3`,
      filePath: tempFilePath,
      success: (res: { fileID: string }) => resolve(res),
      fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '上传失败')),
    });
  });

  return { audioObjectRef: uploaded.fileID, sizeBytes: fileSize, durationMs };
}

/** 100KiB 常量（与服务端 `MAX_BODY_BYTES` 对齐，用于端侧提前拦截）。 */
export const MAX_TEXT_BODY_BYTES = 100 * 1024;

/**
 * 端侧预检：即将发出的请求体（**文本**）是否超 100KiB。
 *
 * 语音接口只传引用，正常情况下永远远小于上限；这条是防「有人把 base64 塞进来」。
 *
 * @param payload 将要发送的对象
 * @returns 是否超限
 */
export function exceedsTextBodyLimit(payload: unknown): boolean {
  try {
    return JSON.stringify(payload).length > MAX_TEXT_BODY_BYTES;
  } catch (error) {
    console.warn('[upload] 序列化失败，按超限处理', error);
    return true;
  }
}
