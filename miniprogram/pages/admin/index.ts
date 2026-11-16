/**
 * 管理台（**电脑版微信大屏双栏**；手机上也能进，自动回落单栏）。
 *
 * 双栏实现
 * -------
 * `resizable: true` 让同一个页面在电脑版微信里拿到更宽的窗口：
 * 宽 ≥ 768px → 左导航 + 右内容；窄 → 导航变顶部横向标签（**入口一个都不少**）。
 *
 * 八个面板（块一个都不能少）
 * -----------------------
 * 档案（商品/客商）· 单据与更正 · 报表 ×3 · 用量与套餐 · 权限 ·
 * 导出 · **R3 待办审批** · 说法训练审核
 *
 * R3 审批的铁律
 * -----------
 * 只有「电脑版微信大屏 + `role=boss` + 二次确认」才能执行；
 * 手机端直接调也会被服务端拦成 `E_R3_PC_REQUIRED`(1206)，
 * 这里额外做一次前置拦截，避免让人白点。
 */

import {
  ApiError,
  approveTask,
  createExport,
  entitlement,
  listDict,
  listExports,
  listTasks,
  login,
  queryDocs,
  queryReport,
  queryStock,
  usage,
  type EntitlementPayload,
  type UsagePayload,
} from '../../services/api';
import { deliverExport, supportedModes } from '../../services/wx-file-forward';

type PanelKey =
  | 'profile'
  | 'docs'
  | 'report'
  | 'usage'
  | 'permission'
  | 'export'
  | 'tasks'
  | 'dict';

interface NavItem {
  key: PanelKey;
  label: string;
  bossOnly?: boolean;
}

const NAV: NavItem[] = [
  { key: 'profile', label: '档案（商品/客商）' },
  { key: 'docs', label: '单据与更正' },
  { key: 'report', label: '报表（3 张）' },
  { key: 'usage', label: '用量与套餐' },
  { key: 'permission', label: '权限' },
  { key: 'export', label: '导出' },
  { key: 'tasks', label: 'R3 待办审批', bossOnly: true },
  { key: 'dict', label: '说法训练审核' },
];

interface AdminData {
  nav: NavItem[];
  active: PanelKey;
  narrow: boolean;
  isBoss: boolean;
  roleCode: string;
  loading: boolean;
  errorText: string;
  message: string;
  rows: { text: string }[];
  usageView: UsagePayload | null;
  entitlementView: EntitlementPayload | null;
  usageNote: string;
  /** R3 待办（字段与 `PendingTask.as_dict()` 一致） */
  tasks: {
    taskId: string;
    intentId: string;
    status: string;
    createdAt: string;
    requiresPc: boolean;
    requiredRole: string;
    hint: string;
  }[];
  /** 个人词表（字段与 `GET /api/v1/dict` 一致） */
  dicts: { dictId: string; rawText: string; mappedType: string; layer: string; version: number }[];
  exports: { jobId: string; scope: string; status: string }[];
  deliverModes: string[];
}

function flatten(item: unknown): { text: string } {
  if (item === null || typeof item !== 'object') {
    return { text: String(item) };
  }
  const entries = Object.entries(item as Record<string, unknown>).filter(
    ([, value]) => value !== null && value !== undefined && value !== ''
  );
  const text = entries.map(([key, value]) => `${key}: ${String(value)}`).join(' · ');
  return { text: text === '' ? '（空）' : text };
}

Page<AdminData, WechatMiniprogram.Page.CustomOption>({
  data: {
    nav: NAV,
    active: 'profile',
    narrow: false,
    isBoss: false,
    roleCode: '',
    loading: false,
    errorText: '',
    message: '',
    rows: [],
    usageView: null,
    entitlementView: null,
    usageNote: '',
    tasks: [],
    dicts: [],
    exports: [],
    deliverModes: [],
  },

  onLoad() {
    this.detectNarrow();
    this.setData({ deliverModes: supportedModes() });
    void this.bootstrap();
  },

  onShow() {
    this.detectNarrow();
  },

  onResize() {
    // 电脑版微信里拖动窗口会触发；窄了就回落单栏
    this.detectNarrow();
  },

  detectNarrow(): void {
    try {
      const info = wx.getWindowInfo();
      this.setData({ narrow: info.windowWidth < 768 });
    } catch (error) {
      console.warn('[admin] getWindowInfo 不可用，按窄屏处理', error);
      this.setData({ narrow: true });
    }
  },

  async bootstrap(): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      const result = await new Promise<{ code: string }>((resolve, reject) => {
        wx.login({
          success: (res: { code: string }) => resolve(res),
          fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '登录失败')),
        });
      });
      const session = await login(result.code);
      // 服务端真实字段是 `role`（见 app/api/v1/auth.py），不是 `role_code`
      this.setData({ isBoss: session.role === 'boss', roleCode: session.role });
    } catch (error) {
      this.setData({ errorText: `登录未完成：${this.messageOf(error)}` });
    }
    await this.loadPanel('profile');
    this.setData({ loading: false });
  },

  onSelect(event: WechatMiniprogram.CustomEvent<{ key: PanelKey }>): void {
    void this.loadPanel(event.detail.key);
  },

  /** 按面板取数（每个面板只调它自己的接口，不预取一堆没用的）。 */
  async loadPanel(key: PanelKey): Promise<void> {
    this.setData({ active: key, loading: true, errorText: '', message: '', rows: [] });
    try {
      if (key === 'profile') {
        const stock = await queryStock('');
        this.setData({ rows: (stock.items ?? []).map(flatten) });
      } else if (key === 'docs') {
        const docs = await queryDocs('');
        this.setData({ rows: (docs.items ?? []).map(flatten) });
      } else if (key === 'report') {
        const kinds: ('today' | 'month' | 'profit')[] = ['today', 'month', 'profit'];
        const parts = await Promise.all(kinds.map((kind) => queryReport(kind)));
        const rows: { text: string }[] = [];
        parts.forEach((part, index) => {
          rows.push({ text: `【${kinds[index]}】` });
          // 报表接口返回的是 `report` 单个对象，不是 items 列表
          if (part.report !== undefined) {
            rows.push(flatten(part.report));
          }
          (part.items ?? []).forEach((item) => rows.push(flatten(item)));
        });
        this.setData({ rows });
      } else if (key === 'usage') {
        const [usagePayload, entitlementPayload] = await Promise.all([usage(), entitlement()]);
        // 两个口径**分开存、分开显示**，绝不拼进同一个进度条
        this.setData({
          usageView: usagePayload.usage,
          entitlementView: entitlementPayload.entitlement,
          usageNote: usagePayload.caliber_note,
        });
      } else if (key === 'permission') {
        this.setData({
          rows: [
            { text: `当前角色：${this.data.roleCode === '' ? '未登录' : this.data.roleCode}` },
            { text: 'R3 高风险动作：仅老板（boss）+ 电脑版微信大屏 + 二次确认可执行' },
            { text: '普通员工：可语音记账、可查库存/欠款；不能审批、不能导出' },
            { text: '没有账号密码体系：身份来自微信登录态（同一 AppID）' },
          ],
        });
      } else if (key === 'export') {
        const payload = await listExports();
        this.setData({
          exports: payload.items.map((item) => ({
            jobId: item.job_id,
            scope: item.scope,
            status: item.status,
          })),
          rows: [{ text: '导出是异步任务；生成后走微信转发/保存/预览，不提供网页下载' }],
        });
      } else if (key === 'tasks') {
        const payload = await listTasks();
        this.setData({
          tasks: payload.items.map((item) => ({
            taskId: item.task_id,
            intentId: item.intent_id,
            status: item.status,
            createdAt: item.created_at,
            requiresPc: item.requires_pc,
            requiredRole: item.required_role,
            hint: item.hint_zh,
          })),
        });
      } else {
        const payload = await listDict();
        this.setData({
          dicts: payload.items.map((item) => ({
            dictId: item.dict_id,
            rawText: item.raw_text,
            mappedType: item.mapped_type,
            layer: item.layer,
            version: item.version,
          })),
        });
      }
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    } finally {
      this.setData({ loading: false });
    }
  },

  /** 新建导出任务。 */
  async onExport(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const scope = String(event.currentTarget.dataset['scope'] ?? 'stock') as
      | 'stock'
      | 'debt'
      | 'docs'
      | 'audit'
      | 'usage';
    try {
      const payload = await createExport(scope);
      const mode = this.data.deliverModes.includes('forward') ? 'forward' : 'preview';
      // 真实结构是 `{export: {job_id, ...}}`（见 app/api/v1/exports.py）
      const delivered = await deliverExport(payload.export.job_id, mode);
      this.setData({ message: delivered.message });
      await this.loadPanel('export');
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** R3 审批：**大屏 + boss + 二次确认**，三重都要满足。 */
  async onApprove(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const taskId = String(event.currentTarget.dataset['task'] ?? '');
    const action = String(event.currentTarget.dataset['action'] ?? 'approve') as 'approve' | 'reject';
    if (this.data.narrow) {
      this.setData({ errorText: 'R3 审批只能在电脑版微信的大屏管理台执行（手机端会被服务端拦成 1206）' });
      return;
    }
    if (!this.data.isBoss) {
      this.setData({ errorText: '只有老板（role=boss）能审批这类高风险动作' });
      return;
    }
    const confirmed = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: action === 'approve' ? '二次确认：确定执行？' : '二次确认：确定驳回？',
        content: '这类操作涉及金额或库存的大幅变动，需要再确认一次。',
        confirmText: '确认',
        cancelText: '再想想',
        success: (res: { confirm: boolean }) => resolve(res.confirm),
        fail: () => resolve(false),
      });
    });
    if (!confirmed) {
      return;
    }
    try {
      const result = await approveTask(taskId, {
        approve: action === 'approve',
        second_confirm: true,
        device: 'pc_bigscreen',
      });
      this.setData({
        message: `${action === 'approve' ? '已执行' : '已驳回'}（${result.status}）${
          result.executed ? '' : `：${result.detail}`
        }`,
      });
      await this.loadPanel('tasks');
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  messageOf(error: unknown): string {
    if (error instanceof ApiError) {
      return `${error.message}${error.errorCode ? `（${error.errorCode}）` : ''}`;
    }
    return error instanceof Error ? error.message : String(error);
  },
});
