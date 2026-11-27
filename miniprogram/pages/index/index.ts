/**
 * 首页（手机端主录入）：语音大按钮 + 降级态状态条 + 最近记录 + 快捷入口。
 *
 * 布局约束
 * -------
 * - 手机端：一列纵向，大按钮在拇指可达区域（屏幕下半部）；
 * - 电脑版微信大屏（`resizable`）：`.wide` 生效后变成「左录入 / 右动态」双栏；
 * - **UI 只暴露单商户**：不出现门店切换、不出现组织/多仓概念。
 */

import type { IAppOption } from '../../app';
import {
  ApiError,
  createVoiceSession,
  getMe,
  health,
  listDocs,
  listPending,
  login,
  parseSession,
  queryReport,
  reverseDoc,
  updatePreferences,
  usage,
  type DocBrief,
  type NoviceStatus,
  type ParsePayload,
} from '../../services/api';
import {
  SPEECH_UNAVAILABLE_HINT,
  SpeechGate,
  speechReasonText,
  type SpeechGateState,
  type SpeechResult,
} from '../../services/speech';
import { ensureLogin } from '../../services/auth';
import { rememberSpokenText, wasSaidRecently } from '../../utils/dedupe';
import { formatDateTime } from '../../utils/format';
import { isWideScreen } from '../../utils/screen';
import type { SheetActionItem } from '../../components/action-sheet/action-sheet';

/** 降级态（L0 正常 → L4 只读）：UI 上必须**看得出来差别**，不能只写进配置。 */
export interface DegradeView {
  level: string;
  title: string;
  hint: string;
  /** 是否还能语音录入（L4 只读时不能） */
  canVoice: boolean;
  /** 是否要展示「省额度」角标 */
  thrifty: boolean;
  tone: 'ok' | 'warn' | 'danger';
}

/** 把后端降级级别映射成 UI 形态（一处定义，页面与测试共用）。 */
export function degradeViewOf(level: string): DegradeView {
  switch (level) {
    case 'L4':
      return {
        level,
        title: '只读模式',
        hint: '额度已用完：现在只能查，不能记账。买套餐后立即恢复。',
        canVoice: false,
        thrifty: true,
        tone: 'danger',
      };
    case 'L3':
      return {
        level,
        title: '严重省钱模式',
        hint: '只剩很少额度：能走规则的绝不调模型，判不准会先问你一句。',
        canVoice: true,
        thrifty: true,
        tone: 'danger',
      };
    case 'L2':
      return {
        level,
        title: '省额度模式',
        hint: '额度紧张：优先用规则识别，必要时才调用模型。',
        canVoice: true,
        thrifty: true,
        tone: 'warn',
      };
    case 'L1':
      return {
        level,
        title: '轻度限速',
        hint: '识别稍慢一点（队列排队中），不影响记账。',
        canVoice: true,
        thrifty: false,
        tone: 'warn',
      };
    default:
      return {
        level: 'L0',
        title: '正常',
        hint: '额度充足，识别走最快通道。',
        canVoice: true,
        thrifty: false,
        tone: 'ok',
      };
  }
}

interface IndexData {
  loading: boolean;
  statusText: string;
  contractText: string;
  degrade: DegradeView;
  recording: boolean;
  durationMs: number;
  bars: number[];
  softLimit: boolean;
  mode: 'hold' | 'tap';
  talkingText: string;
  errorText: string;
  syncErrorText: string;
  /** 同声传译插件是否可用（不可用时降级为键盘录入，大按钮仍可点、只给提示） */
  speechReady: boolean;
  /** 插件不可用的**具体原因**（自检面板显示；空串 = 可用） */
  speechReason: string;
  recent: { id: string; text: string; intent: string; at: string; ok: boolean }[];
  wide: boolean;
  selfCheckOpen: boolean;
  backendVersion: string;
  loginOk: boolean;
  /* —— 今天页（方向 A 暖纸账本）—— */
  greeting: string;
  dateLabel: string;
  periods: { key: string; label: string }[];
  period: string;
  periodLabel: string;
  stats: { sell: string; buy: string; owe: string };
  pendingCount: number;
  fillCount: number;
  flow: {
    id: string;
    docId?: string;
    time: string;
    tag: string;
    tagClass: string;
    title: string;
    meta: string;
    amount: string;
    reversed?: boolean;
  }[];
  talkingMsText: string;
  lastSaid: string;
  thinking: boolean;
  novice: NoviceStatus | null;
  undoBanner: { docId: string; summary: string } | null;
  fontSize: 'big' | 'std';
  sheet: {
    visible: boolean;
    title: string;
    subtitle: string;
    actions: SheetActionItem[];
  };
}

Page<IndexData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    statusText: '连接中…',
    contractText: '',
    degrade: degradeViewOf('L0'),
    recording: false,
    durationMs: 0,
    bars: [],
    softLimit: false,
    mode: 'hold',
    talkingText: '',
    errorText: '',
    syncErrorText: '',
    speechReady: true,
    speechReason: '',
    recent: [],
    wide: false,
    selfCheckOpen: false,
    backendVersion: '',
    loginOk: false,
    greeting: '你好',
    dateLabel: '',
    periods: [
      { key: 'today', label: '今天' },
      { key: 'week', label: '本周' },
      { key: 'month', label: '本月' },
    ],
    period: 'today',
    periodLabel: '今日',
    stats: { sell: '0', buy: '0', owe: '0' },
    pendingCount: 0,
    fillCount: 0,
    flow: [],
    talkingMsText: '',
    lastSaid: '',
    thinking: false,
    novice: null,
    undoBanner: null,
    fontSize: 'big',
    sheet: { visible: false, title: '', subtitle: '', actions: [] },
  },

  sheetFlowRow: null as IndexData['flow'][number] | null,

  gate: null as SpeechGate | null,
  holding: false,
  holdFired: false,
  thinkingCancelled: false,
  undoHideTimer: null as number | null,

  onLoad() {
    // 用局部变量接住，避免 `Page({})` 的 `this` 推断不到具体类型时把回调参数退化成 any。
    const gate: SpeechGate = new SpeechGate();
    this.gate = gate;
    gate.onState((state: SpeechGateState) => {
      this.setData({
        recording: state.recording,
        durationMs: state.durationMs,
        bars: state.bars,
        softLimit: state.softLimitReached,
        // 插件的中间结果只用来做「正在说：…」的实时反馈，不参与任何落账判断
        talkingText: state.recording && state.partialText !== '' ? `正在说：${state.partialText}` : '',
        talkingMsText: state.recording ? `${(state.durationMs / 1000).toFixed(1)}s` : '',
      });
    });
    gate.onAutoStop((said: SpeechResult | null) => {
      if (said !== null) {
        void this.submitSpokenResult(said);
      }
    });
    // 插件没配好：**不崩不白屏**，只是把语音降级成键盘（大按钮仍可点，点了给同一句提示）
    this.setData({
      speechReady: gate.available,
      // 把**具体原因**也带出来：「点大按钮没反应」有好几种完全不同的原因，
      // 修法也完全不同（版本号 / 后台审核 / 隐私声明 / 微信版本），
      // 只说「未启用」的话，开发者只能反复试。
      speechReason: gate.available ? '' : speechReasonText(gate.unavailableReason),
      errorText: gate.available ? '' : SPEECH_UNAVAILABLE_HINT,
    });
    void this.bootstrap();
  },

  onShow() {
    const app = getApp<IAppOption>();
    app.detectScreen();
    const savedSize = (wx.getStorageSync('dsh:fontSize') as string) === 'std' ? 'std' : 'big';
    this.setData({ wide: app.globalData.screen === 'wide' || isWideScreen(), fontSize: savedSize });
    this.refreshHeader();

    // Tab 中间键：回到今天页并请求开始/结束说话
    const bar = (this as unknown as { getTabBar?: () => { setData: (d: object) => void } }).getTabBar?.();
    bar?.setData({ selected: 0 });

    const global = getApp() as unknown as { globalData: { voiceRequested?: boolean } };
    if (global.globalData.voiceRequested === true) {
      global.globalData.voiceRequested = false;
      void this.onVoiceTap();
    }

    const graduated = wx.getStorageSync('dsh:justGraduated') as NoviceStatus | '' | undefined;
    if (graduated && typeof graduated === 'object' && graduated.just_graduated) {
      wx.removeStorageSync('dsh:justGraduated');
      this.showGraduationModal(graduated);
    }

    void this.loadDashboard();
  },

  onResize() {
    const app = getApp<IAppOption>();
    app.detectScreen();
    this.setData({ wide: isWideScreen() });
  },

  async loadDashboard(): Promise<void> {
    try {
      await ensureLogin();
    } catch (error) {
      console.warn('[index] 登录未就绪', error);
      this.setData({ syncErrorText: '网络未就绪，点右侧重试刷新流水' });
      return;
    }
    await Promise.all([this.refreshFlow(), this.refreshReport()]);
    await Promise.all([this.refreshPending(), this.refreshMe()]);
  },

  async onPullDownRefresh(): Promise<void> {
    try {
      await this.loadDashboard();
    } finally {
      wx.stopPullDownRefresh();
    }
  },

  onRetryDashboard(): void {
    void this.loadDashboard();
  },

  async refreshMe(): Promise<void> {
    try {
      const me = await getMe();
      wx.setStorageSync('dsh:novice', me.novice);
      this.setData({ novice: me.novice });
    } catch (error) {
      console.warn('[index] 拉取新手期状态失败', error);
    }
  },

  showGraduationModal(novice: NoviceStatus): void {
    const pct = Math.round((novice.first_pass_rate || 0) * 100);
    wx.showModal({
      title: `已一起记满 ${novice.target} 笔`,
      content: `首遍准确率 ${pct}%。以后有把握的简单单可以直接记账（5 秒内可撤销），不放心的照样弹确认卡。`,
      confirmText: '好的',
      cancelText: '每笔都确认',
      success: (res) => {
        if (!res.confirm) {
          void updatePreferences({ auto_post_enabled: false }).then(() => this.refreshMe());
        }
      },
    });
  },

  async refreshFlow(): Promise<void> {
    try {
      const payload = await listDocs(20);
      const items = payload.items ?? [];
      if (items.length === 0 && this.data.recent.length > 0) {
        this.setData({ flow: this.buildFlow(this.data.recent) });
        return;
      }
      const flow = items.map((doc: DocBrief) => {
        const isSale = doc.intent_id.startsWith('sales.') || doc.doc_type === 'sales';
        const isCredit = doc.payment_status === '未结';
        const isReturn = doc.intent_id.includes('return');
        const tag = isReturn ? '退' : isCredit ? '赊' : isSale ? '卖' : '进';
        const tagClass = isReturn
          ? 'tg--back'
          : isCredit
            ? 'tg--owe'
            : isSale
              ? 'tg--sell'
              : 'tg--in';
        const reversed = doc.status === 'reversed';
        const amountYuan = (Number(doc.total_amount_fen || 0) / 100)
          .toFixed(2)
          .replace(/\.00$/, '');
        return {
          id: doc.doc_id,
          docId: doc.doc_id,
          time: doc.biz_date.slice(5),
          tag,
          tagClass,
          title: `${doc.doc_no} · ${doc.payment_status}`,
          meta: reversed ? '已撤' : '已记账',
          amount: amountYuan,
          reversed,
        };
      });
      this.setData({ flow, syncErrorText: '' });
    } catch (error) {
      console.warn('[index] 拉取流水失败', error);
      this.setData({ syncErrorText: '网络波动，今日流水暂未刷新' });
    }
  },

  /** 今日/本周/本月三个数：来自只读报表 `/query/report?period=...`（不编数字）。 */
  async refreshReport(): Promise<void> {
    try {
      const p = (this.data.period === 'week' || this.data.period === 'month'
        ? this.data.period
        : 'today') as 'today' | 'week' | 'month';
      const payload = await queryReport(p, p);
      const report = (payload.report ?? {}) as Record<string, unknown>;
      const yuan = (key: string): string => {
        const fen = Number(report[key] ?? 0);
        if (!Number.isFinite(fen)) {
          return '0';
        }
        const value = fen / 100;
        return Number.isInteger(value) ? String(value) : value.toFixed(2);
      };
      this.setData({
        stats: {
          sell: yuan('sales_fen'),
          buy: report.cost_hidden === true || report.purchase_fen === null ? '--' : yuan('purchase_fen'),
          owe: yuan('receivable_open_fen'),
        },
      });
    } catch (error) {
      console.warn('[index] 取今日报表失败', error);
      if (this.data.syncErrorText === '') {
        this.setData({ syncErrorText: '网络波动，今日汇总暂未刷新' });
      }
    }
  },

  /** 待处理角标：没确认的卡有几张。 */
  async refreshPending(): Promise<void> {
    try {
      const payload = await listPending();
      this.setData({
        pendingCount: payload.items.length,
        fillCount: payload.items.filter((row) => (row.gaps ?? []).length > 0).length,
      });
    } catch (error) {
      console.warn('[index] 取待处理失败', error);
    }
  },

  /** 问候语与日期（原型：早上好，王姐 / 11月20日 周五 · 王姐便利店）。 */
  refreshHeader(): void {
    const now = new Date();
    const hour = now.getHours();
    const greeting = hour < 6 ? '凌晨好' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
    const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.getDay()];
    this.setData({
      greeting,
      dateLabel: `${now.getMonth() + 1}月${now.getDate()}日 ${week}`,
    });
  },

  /** 最近记录 → 流水行（意图文字映射成标签，时间取 HH:mm）。 */
  buildFlow(recent: IndexData['recent']): IndexData['flow'] {
    return recent.map((item) => {
      const tag = item.intent || '记';
      const tagClass = tag.includes('卖')
        ? 'tg--sell'
        : tag.includes('赊') || tag.includes('欠')
          ? 'tg--owe'
          : tag.includes('退')
            ? 'tg--back'
            : tag.includes('盘')
              ? 'tg--count'
              : tag.includes('收')
                ? 'tg--pay'
                : 'tg--in';
      const matched = /(\d{2}:\d{2})/.exec(item.at);
      return {
        id: item.id,
        time: matched ? matched[1] : '',
        tag,
        tagClass,
        title: item.text,
        meta: item.ok ? '已记' : '待确认',
        amount: '',
      };
    });
  },

  onUnload() {
    if (this.undoHideTimer !== null) {
      clearTimeout(this.undoHideTimer);
      this.undoHideTimer = null;
    }
    this.gate?.dispose();
    this.gate = null;
  },

  /** 登录 + 健康检查 + 额度（三件事都失败也不阻塞录入，只提示）。 */
  async bootstrap(): Promise<void> {
    // 不清掉「语音未启用」这句：它由 onLoad 按插件可用性设定，清掉就等于骗用户「能说」
    this.setData({ loading: true, errorText: this.data.speechReady ? '' : SPEECH_UNAVAILABLE_HINT });
    try {
      // 和其它页面共用同一次登录，避免首页与货品页并发各登录一次
      await ensureLogin();
      this.setData({ loginOk: true });
      const payload = await health();
      this.setData({
        statusText: payload.status === 'ok' ? '后端正常' : '后端降级',
        backendVersion: payload.version,
        contractText: `契约 ${payload.contract.intents} 意图 / ${payload.contract.slots} 槽位 / ${payload.contract.error_codes} 错误码`,
      });
    } catch (error) {
      this.setData({ statusText: `连接失败：${this.messageOf(error)}` });
    }
    await this.refreshUsage();
    this.setData({ loading: false });
  },

  async doLogin(): Promise<void> {
    try {
      const result = await new Promise<{ code: string }>((resolve, reject) => {
        wx.login({
          success: (res: { code: string }) => resolve(res),
          fail: (error: { errMsg?: string }) => reject(new Error(error.errMsg ?? '登录失败')),
        });
      });
      await login(result.code);
      this.setData({ loginOk: true });
    } catch (error) {
      // 本地联调（dev 通道）可能没有真实 code，登录失败不阻塞其它功能
      this.setData({ loginOk: false, errorText: `登录未完成：${this.messageOf(error)}` });
    }
  },

  async refreshUsage(): Promise<void> {
    try {
      // `/api/v1/usage` 的真实结构是 `{usage, ledger_rows, caliber_note}`
      const snapshot = await usage();
      this.setData({ degrade: degradeViewOf(snapshot.usage.degrade_level) });
    } catch (error) {
      console.warn('[index] 取用量失败', error);
    }
  },

  /**
   * 大按钮：开始说话。
   *
   * 现在是**异步**的：按下之后要先过隐私授权（可能要弹官方隐私弹窗、等用户点同意），
   * 通过了才真的开始录音。所以这里返回 Promise，让「按住说」模式能正确处理
   * 「按下 → 授权中 → 抬手」的时序（见 `SpeechGate.stop` 里的说明）。
   */
  async onStart(): Promise<void> {
    if (!this.data.speechReady) {
      // 插件没配好：按钮**仍可点**，点了给一句能照做的提示（不崩、不白屏、不静默）
      wx.showToast({ title: SPEECH_UNAVAILABLE_HINT, icon: 'none' });
      return;
    }
    if (!this.data.degrade.canVoice) {
      wx.showToast({ title: '只读模式下不能记账', icon: 'none' });
      return;
    }
    if (this.gate === null) {
      return;
    }
    this.setData({ errorText: '' });
    const outcome = await this.gate.start();
    if (!outcome.granted) {
      // 隐私被拒 → 保留可重试状态（不永久锁死 speechReady），把原因和提示写清楚
      this.setData({
        speechReady: outcome.reason === 'privacy_denied' ? this.gate.available : false,
        speechReason: speechReasonText(
          outcome.reason === 'privacy_denied' ? 'privacy_denied' : 'privacy_not_declared',
        ),
        errorText: outcome.hint,
      });
    }
  },

  /** 大按钮：结束说话 → 提交识别结果。 */
  async onStop(): Promise<void> {
    if (this.gate === null) {
      return;
    }
    const said = await this.gate.stop();
    if (said === null) {
      return; // 被门禁丢弃（太短/没听到/插件没给文字），不发请求、不扣次
    }
    await this.submitSpokenResult(said);
  },

  /** 提交已识别文字：建会话 → 校验额度 → 解析（手动停止与自动到时共用）。 */
  async submitSpokenResult(said: SpeechResult): Promise<void> {
    // 60 秒内说过同一句 → **先问再发**，别静默吞掉。
    // 此时还没建会话、还没扣额度：用户点「不记了」是零代价的。
    if (wasSaidRecently(said.text) && !(await this.confirmRealRepeat())) {
      this.setData({ talkingText: '' });
      return;
    }
    // 确认要发了才记进去（用户取消的那次不占窗口）
    rememberSpokenText(said.text);
    this.thinkingCancelled = false;
    this.setData({ loading: true, talkingText: '识别中…', thinking: true });
    try {
      // ① 建会话（拿会话 id 与令牌桶额度）→ ② 解析：只传文字，音频不出手机
      const session = await createVoiceSession({
        duration_ms: said.durationMs,
        mode: this.data.mode,
      });
      if (this.thinkingCancelled) {
        return;
      }
      if (session.session_id === null || session.session_id === '') {
        const msg = session.message_zh || '暂时说得太快了，请稍等一会再说';
        this.setData({ errorText: msg });
        wx.showToast({ title: msg.slice(0, 20), icon: 'none' });
        return;
      }
      const hasRef = typeof said.audioObjectRef === 'string' && said.audioObjectRef !== '';
      const parsed = await parseSession(session.session_id, {
        asr_text: said.text !== '' ? said.text : undefined,
        audio_object_ref: hasRef ? said.audioObjectRef : undefined,
        duration_ms: said.durationMs,
      });
      if (this.thinkingCancelled) {
        return;
      }
      this.handleParsed(parsed);
    } catch (error) {
      if (!this.thinkingCancelled) {
        this.setData({ errorText: this.messageOf(error) });
        await this.refreshUsage();
      }
    } finally {
      this.setData({ loading: false, talkingText: '', thinking: false });
    }
  },

  /** 思考中点「取消」：收起面板并忽略本次后续返回，不再重新启动录音。 */
  onCancelThinking(): void {
    this.thinkingCancelled = true;
    this.setData({ thinking: false, loading: false, talkingText: '' });
  },

  /** 5 秒内一键撤回直记单据。 */
  async onUndoAutoPosted(): Promise<void> {
    const banner = this.data.undoBanner;
    if (banner === null || !banner.docId) {
      return;
    }
    if (this.undoHideTimer !== null) {
      clearTimeout(this.undoHideTimer);
      this.undoHideTimer = null;
    }
    try {
      await reverseDoc(banner.docId, '直记5秒内撤回');
      this.setData({ undoBanner: null });
      wx.showToast({ title: '已撤回', icon: 'success' });
      void this.loadDashboard();
    } catch (error) {
      this.setData({ undoBanner: null, errorText: this.messageOf(error) });
    }
  },

  /**
   * 「刚才这句话刚说过」时的追问。
   *
   * 真实重复的生意（老板确实要连记两笔同样的）**不能被静默吃掉**——那等于凭空丢一单。
   * 所以这里不直接拦，而是让用户自己选：
   * - 「是，再记一笔」→ 返回 true，照常发请求；
   * 「不记了」/ 关闭弹窗 → 返回 false，一请求都不发（没建会话、没扣额度）。
   */
  confirmRealRepeat(): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '刚才记过一句一样的',
        content: '是又记了一笔，还是刚才点重了？',
        confirmText: '是，再记一笔',
        cancelText: '不记了',
        success: (res) => {
          resolve(Boolean(res.confirm));
        },
        fail: () => {
          resolve(false);
        },
      });
    });
  },

  /** 切换触发模式。 */
  onModeChange(event: WechatMiniprogram.CustomEvent<{ mode: 'hold' | 'tap' }>): void {
    this.setData({ mode: event.detail.mode });
  },

  /** 一句话处理完：直记 / 出卡 / 澄清 / 只读 / 待办。 */
  handleParsed(parsed: ParsePayload): void {
    if (parsed.novice !== undefined) {
      wx.setStorageSync('dsh:novice', parsed.novice);
      this.setData({ novice: parsed.novice });
    }
    if (parsed.route === 'auto_posted' && parsed.result !== undefined) {
      this.pushRecent(parsed.asr_text, parsed.parse_result?.intent ?? '直记', true);
      const docId = parsed.result.doc_id ?? '';
      const summary = `${parsed.asr_text.slice(0, 18)} (${parsed.result.doc_no ?? '已落账'})`;
      if (this.undoHideTimer !== null) {
        clearTimeout(this.undoHideTimer);
      }
      this.setData({ undoBanner: docId ? { docId, summary } : null });
      if (docId) {
        const windowMs = (parsed.undo_window_seconds ?? 5) * 1000;
        this.undoHideTimer = setTimeout(() => {
          this.setData({ undoBanner: null });
          this.undoHideTimer = null;
        }, windowMs) as unknown as number;
      }
      void this.loadDashboard();
      if (parsed.novice?.just_graduated) {
        this.showGraduationModal(parsed.novice);
      }
      return;
    }
    if (parsed.route === 'card' && parsed.card !== null && parsed.card !== undefined) {
      this.pushRecent(parsed.asr_text, parsed.parse_result?.intent ?? '交易', true);
      wx.navigateTo({ url: `/pages/card/index?cardId=${parsed.card.meta.card_id}` });
      return;
    }
    if (parsed.route === 'clarify' && parsed.clarify !== null && parsed.clarify !== undefined) {
      // 澄清内容不在卡片接口里：先暂存，确认卡页按 sessionId 取回，避免进去后只剩一句红字
      wx.setStorageSync('dsh:pendingClarify', {
        sessionId: parsed.session_id,
        clarify: parsed.clarify,
      });
      wx.navigateTo({ url: `/pages/card/index?sessionId=${parsed.session_id}` });
      return;
    }
    if (parsed.route === 'static' && parsed.static_answer !== undefined) {
      wx.showModal({ title: '提示', content: parsed.static_answer, showCancel: false });
      return;
    }
    if (parsed.route === 'answer') {
      const items = parsed.answer?.items ?? [];
      const first = items.length > 0 ? JSON.stringify(items[0]) : '没有查到';
      wx.showModal({ title: '查询结果', content: first.slice(0, 300), showCancel: false });
      return;
    }
    if (parsed.route === 'pending_task') {
      wx.showModal({
        title: '这条要在电脑上办',
        content: '这类操作要老板在电脑版微信的大屏管理台确认，手机上不会直接记账。',
        showCancel: false,
      });
      return;
    }
    if (parsed.error_code !== undefined && parsed.error_code !== '') {
      this.setData({ errorText: `没处理成功（${parsed.error_code}）` });
      return;
    }
    this.setData({ errorText: '没听出要记什么，换个说法或点「手动记账」' });
  },

  onGoManual(): void {
    wx.navigateTo({ url: '/pages/manual/index' });
  },

  onGoDeliveryNote(): void {
    wx.navigateTo({ url: '/pages/delivery-note/index' });
  },

  onGoPending(): void {
    wx.navigateTo({ url: '/pages/pending/index' });
  },

  onGoQuery(): void {
    wx.navigateTo({ url: '/pages/query/index' });
  },

  onGoPay(): void {
    wx.navigateTo({ url: '/pages/pay/index' });
  },

  onGoAdmin(): void {
    wx.navigateTo({ url: '/pages/admin/index' });
  },

  /* —— 今天页交互 —— */

  onPeriod(event: WechatMiniprogram.TouchEvent): void {
    const key = String(event.currentTarget.dataset.key ?? 'today');
    this.setData(
      {
        period: key,
        periodLabel: key === 'today' ? '今日' : key === 'week' ? '本周' : '本月',
      },
      () => {
        void this.refreshReport();
      }
    );
  },

  /** 点流水一行：真单据支持去改单或撤销。 */
  onOpenFlow(event: WechatMiniprogram.TouchEvent): void {
    const id = String(event.currentTarget.dataset.id ?? '');
    const row = this.data.flow.find((item) => item.id === id);
    if (row === undefined) {
      return;
    }
    if (!row.docId) {
      wx.showModal({ title: row.tag, content: row.title, showCancel: false });
      return;
    }
    if (row.reversed) {
      wx.showToast({ title: '这笔已经撤销过了', icon: 'none' });
      return;
    }
    this.sheetFlowRow = row;
    this.setData({
      sheet: {
        visible: true,
        title: row.title,
        subtitle: `${row.tag}${row.amount ? ` · ¥${row.amount}` : ''}${row.time ? ` · ${row.time}` : ''}`,
        actions: [
          { key: 'edit', label: '改这笔账', desc: '调整数量、单价后一步重记', tone: 'primary' },
          { key: 'reverse', label: '撤销这笔账', desc: '保留原单并标为已撤，自动反做库存与账款', tone: 'danger' },
        ],
      },
    });
  },

  onSheetClose(): void {
    this.sheetFlowRow = null;
    this.setData({ 'sheet.visible': false });
  },

  onSheetSelect(event: WechatMiniprogram.CustomEvent<{ index: number; key: string }>): void {
    const row = this.sheetFlowRow;
    const { key } = event.detail;
    this.onSheetClose();
    if (!row || !row.docId) {
      return;
    }
    const docId = row.docId;
    if (key === 'edit') {
      wx.navigateTo({ url: `/pages/correct/index?docId=${docId}` });
    } else if (key === 'reverse') {
      wx.showModal({
        title: '确认撤销',
        content: `要撤销「${row.title}」吗？原单会保留并标为已撤，库存与账款自动反做。`,
        confirmText: '确认撤销',
        success: (modalRes) => {
          if (modalRes.confirm) {
            void reverseDoc(docId, '首页撤销').then(
              () => {
                wx.showToast({ title: '已撤销', icon: 'success' });
                void this.loadDashboard();
              },
              (err: unknown) => {
                this.setData({ errorText: this.messageOf(err) });
              }
            );
          }
        },
      });
    }
  },

  onOpenSelfCheck(): void {
    this.setData({ selfCheckOpen: !this.data.selfCheckOpen });
  },

  /** 轻点：开始 / 结束。 */
  async onVoiceTap(): Promise<void> {
    if (this.holdFired) {
      this.holdFired = false;
      return;
    }
    if (this.data.recording) {
      await this.onStop();
      return;
    }
    await this.onStart();
  },

  /** 按住说：按下开始，松手结束（照顾微信习惯）。 */
  async onVoiceHoldStart(): Promise<void> {
    this.holdFired = true;
    this.holding = true;
    if (!this.data.recording) {
      await this.onStart();
    }
  },

  async onVoiceHoldEnd(): Promise<void> {
    if (!this.holding) {
      return;
    }
    this.holding = false;
    if (this.data.recording) {
      await this.onStop();
    }
  },

  onToggleSelfCheck(): void {
    this.setData({ selfCheckOpen: !this.data.selfCheckOpen });
  },

  messageOf(error: unknown): string {
    if (error instanceof ApiError) {
      return error.message || error.errorCode;
    }
    if (error instanceof Error) {
      return error.message;
    }
    return String(error);
  },

  /** 记录一条最近操作（本地展示，不做业务判断）。 */
  pushRecent(text: string, intent: string, ok: boolean): void {
    const item = { id: `${Date.now()}`, text, intent, at: formatDateTime(new Date().toISOString()), ok };
    const recent = [item, ...this.data.recent].slice(0, 5);
    this.setData({ recent, flow: this.buildFlow(recent) });
  },
});
