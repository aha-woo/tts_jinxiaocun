/**
 * 管理台（**电脑版微信大屏双栏**；手机上也能进，自动回落单栏）。
 *
 * 双栏实现
 * -------
 * `resizable: true` 让同一个页面在电脑版微信里拿到更宽的窗口：
 * 宽 ≥ 768px → 左导航 + 右内容（结构化表格与 KPI 经营报表）；窄 → 导航变顶部横向标签（**入口一个都不少**）。
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
  getMe,
  listDict,
  listExports,
  listTasks,
  queryDocs,
  queryReport,
  queryStock,
  usage,
  type EntitlementPayload,
  type UsagePayload,
} from '../../services/api';
import { ensureLogin } from '../../services/auth';
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

interface AdminStockRow {
  id: string;
  name: string;
  specUnit: string;
  stock: string;
  costText: string;
  statusText: string;
  negative: boolean;
  trackBatch: boolean;
}

interface AdminDocRow {
  docId: string;
  docNo: string;
  bizDate: string;
  typeLabel: string;
  paymentLabel: string;
  amountText: string;
  statusLabel: string;
  reversed: boolean;
}

interface AdminReportCard {
  key: 'today' | 'week' | 'month';
  title: string;
  bizDate: string;
  sales: string;
  purchase: string;
  profit: string;
  docCount: number;
  receivable: string;
  payable: string;
  missingCost: number;
}

interface AdminTopProduct {
  rank: number;
  name: string;
  qty: string;
  amount: string;
}

interface AdminData {
  nav: NavItem[];
  active: PanelKey;
  narrow: boolean;
  isBoss: boolean;
  roleCode: string;
  /** 角色中文名（boss→老板），不给用户看代码里的英文枚举 */
  roleLabel: string;
  loading: boolean;
  errorText: string;
  message: string;
  rows: { text: string }[];
  stockRows: AdminStockRow[];
  docRows: AdminDocRow[];
  reportCards: AdminReportCard[];
  topProducts: AdminTopProduct[];
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
  dicts: {
    dictId: string;
    rawText: string;
    targetText: string;
    typeLabel: string;
    mappedType: string;
    layer: string;
    version: number;
  }[];
  exports: { jobId: string; scope: string; status: string }[];
  deliverModes: string[];
}

const DOC_TYPE_LABELS: Record<string, string> = {
  sales: '卖货',
  purchase: '进货',
  sales_return: '销退',
  purchase_return: '采退',
  receipt: '收款',
  payment: '付款',
  count_adjust: '盘点调整',
  waste: '报损',
};

const DOC_PAYMENT_LABELS: Record<string, string> = {
  paid: '已结清',
  unpaid: '赊账未结',
  partial: '部分结算',
  已结: '已结清',
  未结: '赊账未结',
  部分: '部分结算',
};

const DOC_STATUS_LABELS: Record<string, string> = {
  confirmed: '已入账',
  posted: '已入账',
  reversed: '已撤销',
  draft: '草稿',
};

/** 角色代码 → 中文（界面不出现 boss / manager 这类英文枚举）。 */
export function roleLabelOf(code: string): string {
  const table: Record<string, string> = {
    boss: '老板',
    manager: '店长',
    clerk: '店员',
    readonly: '只读',
    accountant: '会计',
  };
  return table[code] ?? (code === '' ? '未登录' : code);
}

function formatDateTimeMinute(raw: unknown): string {
  const s = typeof raw === 'string' ? raw.trim() : '';
  if (s === '') {
    return '—';
  }
  return s.replace('T', ' ').slice(0, 16);
}

function fenDisplay(value: unknown, fallback = '¥0.00'): string {
  if (value === null || value === undefined) {
    return fallback;
  }
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return fallback;
  }
  return `¥${(n / 100).toFixed(2)}`;
}

function toStockRow(item: unknown, index: number): AdminStockRow {
  const o = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>;
  const id = String(o['product_id'] ?? o['id'] ?? `s${index}`);
  const name = String(o['product_name'] ?? o['name'] ?? `商品 ${index + 1}`);
  const spec = String(o['spec'] ?? '');
  const unit = String(o['base_unit'] ?? o['unit'] ?? '');
  const specUnit = [spec, unit].filter(Boolean).join(' / ') || '—';
  const stock = String(o['qty_display'] ?? o['qty_base'] ?? '0');
  const costHidden = Boolean(o['cost_hidden']);
  const avgFen = o['avg_cost_fen'];
  const lastFen = o['last_purchase_price_fen'];
  const costVal = avgFen ?? lastFen;
  const costText = costHidden
    ? '仅老板可见'
    : costVal !== null && costVal !== undefined
      ? fenDisplay(costVal)
      : '待补进价';
  const negative = Boolean(o['negative_flag']);
  const trackBatch = Boolean(o['track_batch']);
  const statusText = negative ? '负库存预警' : trackBatch ? '分批管理' : '正常';
  return { id, name, specUnit, stock, costText, statusText, negative, trackBatch };
}

function toDocRow(item: unknown, index: number): AdminDocRow {
  const o = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>;
  const docId = String(o['doc_id'] ?? `doc-${index}`);
  const docNo = String(o['doc_no'] ?? '—');
  const bizDate = String(o['biz_date'] ?? '—');
  const rawType = String(o['doc_type'] ?? '');
  const typeLabel = DOC_TYPE_LABELS[rawType] ?? (rawType || '单据');
  const rawPay = String(o['payment_status'] ?? '');
  const paymentLabel = DOC_PAYMENT_LABELS[rawPay] ?? (rawPay || '—');
  const amountText = String(o['total_amount_display'] ?? fenDisplay(o['total_amount_fen']));
  const rawStatus = String(o['status'] ?? '');
  const reversed = rawStatus === 'reversed';
  const statusLabel = DOC_STATUS_LABELS[rawStatus] ?? (rawStatus || '—');
  return { docId, docNo, bizDate, typeLabel, paymentLabel, amountText, statusLabel, reversed };
}

function toReportCard(key: 'today' | 'week' | 'month', title: string, reportObj: unknown): AdminReportCard {
  const r = (reportObj && typeof reportObj === 'object' ? reportObj : {}) as Record<string, unknown>;
  return {
    key,
    title,
    bizDate: String(r['biz_date'] ?? ''),
    sales: String(r['sales_display'] ?? fenDisplay(r['sales_fen'])),
    purchase: String(r['purchase_display'] ?? fenDisplay(r['purchase_fen'])),
    profit:
      r['gross_profit_display'] !== undefined && r['gross_profit_display'] !== null
        ? String(r['gross_profit_display'])
        : r['gross_profit_fen'] === null
          ? '缺成本或权限'
          : fenDisplay(r['gross_profit_fen'], '—'),
    docCount: Number(r['doc_count'] ?? 0),
    receivable: fenDisplay(r['receivable_open_fen']),
    payable: fenDisplay(r['payable_open_fen']),
    missingCost: Number(r['missing_cost_count'] ?? 0),
  };
}

Page<AdminData, WechatMiniprogram.Page.CustomOption>({
  data: {
    nav: NAV,
    active: 'profile',
    narrow: false,
    isBoss: false,
    roleCode: '',
    roleLabel: '未登录',
    loading: false,
    errorText: '',
    message: '',
    rows: [],
    stockRows: [],
    docRows: [],
    reportCards: [],
    topProducts: [],
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
      await ensureLogin();
      const me = await getMe();
      this.setData({
        isBoss: me.role === 'boss',
        roleCode: me.role,
        roleLabel: roleLabelOf(me.role),
      });
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
  async loadPanel(key: PanelKey, preserveMessage = false): Promise<void> {
    this.setData({
      active: key,
      loading: true,
      errorText: '',
      ...(preserveMessage ? {} : { message: '' }),
      rows: [],
      stockRows: [],
      docRows: [],
      reportCards: [],
      topProducts: [],
    });
    try {
      if (key === 'profile') {
        const stock = await queryStock('');
        this.setData({ stockRows: (stock.items ?? []).map(toStockRow) });
      } else if (key === 'docs') {
        const docs = await queryDocs('');
        this.setData({ docRows: (docs.items ?? []).map(toDocRow) });
      } else if (key === 'report') {
        const [todayRes, weekRes, monthRes, topRes] = await Promise.all([
          queryReport('today'),
          queryReport('week'),
          queryReport('month'),
          queryReport('top'),
        ]);
        const reportCards = [
          toReportCard('today', '今日经营与毛利', todayRes.report),
          toReportCard('week', '本周经营', weekRes.report),
          toReportCard('month', '本月经营与毛利', monthRes.report),
        ];
        const rawTop = ((topRes as { top_products?: unknown[] }).top_products ?? []) as Record<string, unknown>[];
        const topProducts: AdminTopProduct[] = rawTop.map((item, idx) => ({
          rank: idx + 1,
          name: String(item['product_name'] ?? '未命名'),
          qty: String(item['qty_base'] ?? '0'),
          amount: String(item['amount_display'] ?? fenDisplay(item['amount_fen'])),
        }));
        this.setData({ reportCards, topProducts });
      } else if (key === 'usage') {
        const [usagePayload, entitlementPayload] = await Promise.all([usage(), entitlement()]);
        const rawUsage = usagePayload.usage;
        const rawEnt = entitlementPayload.entitlement;
        const planCode = String(rawEnt?.plan_code ?? 'free');
        const planFallback =
          planCode === 'free' ? '免费体验版' : planCode === '30day' ? '30 天权益版' : planCode;
        // 两个口径**分开存、分开显示**，绝不拼进同一个进度条
        this.setData({
          usageView: {
            ...rawUsage,
            used: Number(rawUsage?.used ?? rawUsage?.used_count ?? 0),
            quota: Number(rawUsage?.quota ?? rawUsage?.free_quota ?? 0),
          },
          entitlementView: {
            ...rawEnt,
            plan_name: String(rawEnt?.plan_name || planFallback),
            remaining_quota: Number(rawEnt?.remaining_quota ?? rawEnt?.remaining ?? 0),
            days_left: Number(rawEnt?.days_left ?? 0),
          },
          usageNote: usagePayload.caliber_note,
        });
      } else if (key === 'permission') {
        this.setData({
          rows: [
            { text: `当前角色：${this.data.roleLabel}` },
            { text: '重要高风险操作：仅老板在电脑版微信大屏二次确认后可执行' },
            { text: '店员权限：可语音或手动记账、可查库存与欠款；默认隐藏进价与毛利，不能审批或导出' },
            { text: '登录方式：使用当前微信账号自动绑定小店，无需设置账号密码' },
          ],
        });
      } else if (key === 'export') {
        const scopeMap: Record<string, string> = {
          stock: '库存表',
          debt: '欠款对账表',
          docs: '单据流水表',
          audit: '操作记录',
          usage: '用量账单',
        };
        const statusMap: Record<string, string> = {
          done: '已生成',
          ready: '可转发',
          pending: '生成中',
          failed: '生成失败',
        };
        const payload = await listExports();
        this.setData({
          exports: payload.items.map((item) => ({
            jobId: item.job_id,
            scope: scopeMap[item.scope] ?? item.scope,
            status: statusMap[item.status] ?? item.status,
          })),
          rows: [{ text: '导出完成后会直接调用微信转发或保存，无需网页下载' }],
        });
      } else if (key === 'tasks') {
        const taskStatusMap: Record<string, string> = {
          pending: '待审批',
          approved: '已执行',
          rejected: '已驳回',
        };
        const payload = await listTasks();
        this.setData({
          tasks: payload.items.map((item) => ({
            taskId: item.task_id,
            intentId: item.intent_id,
            status: taskStatusMap[item.status] ?? item.status,
            createdAt: formatDateTimeMinute(item.created_at),
            requiresPc: item.requires_pc,
            requiredRole: roleLabelOf(item.required_role),
            hint: item.hint_zh,
          })),
        });
      } else {
        const typeLabels: Record<string, string> = {
          product: '商品叫法',
          partner: '往来单位',
          number: '数字读法',
          unit: '单位说法',
          intent: '动作说法',
        };
        const payload = await listDict();
        this.setData({
          dicts: payload.items.map((item) => ({
            dictId: item.dict_id,
            rawText: item.raw_text,
            targetText: item.mapped_name || '未指定对象',
            typeLabel: item.mapped_type_label || typeLabels[item.mapped_type] || item.mapped_type,
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
      await this.loadPanel('export', true);
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** R3 审批：**大屏 + boss + 二次确认**，三重都要满足。 */
  async onApprove(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const taskId = String(event.currentTarget.dataset['task'] ?? '');
    const action = String(event.currentTarget.dataset['action'] ?? 'approve') as 'approve' | 'reject';
    if (this.data.narrow) {
      this.setData({ errorText: '重要操作审批只能在电脑版微信的大屏管理台执行' });
      return;
    }
    if (!this.data.isBoss) {
      this.setData({ errorText: '只有老板可以审批这类重要操作' });
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
      const dec = result.decision;
      const statusText =
        dec?.status === 'approved'
          ? '已执行'
          : dec?.status === 'rejected'
            ? '已驳回'
            : String(dec?.status ?? '已处理');
      const detailText = dec?.detail ? `：${dec.detail}` : '';
      this.setData({
        message: `${statusText}${detailText}`,
      });
      await this.loadPanel('tasks', true);
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  messageOf(error: unknown): string {
    if (error instanceof ApiError) {
      return error.message;
    }
    return error instanceof Error ? error.message : String(error);
  },
});
