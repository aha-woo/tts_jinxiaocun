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
  health,
  login,
  parseSession,
  usage,
  type ParsePayload,
} from '../../services/api';
import { SPEECH_UNAVAILABLE_HINT, SpeechGate, speechReasonText, type SpeechGateState } from '../../services/speech';
import { rememberSpokenText, wasSaidRecently } from '../../utils/dedupe';
import { formatDateTime } from '../../utils/format';

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
  /** 同声传译插件是否可用（不可用时降级为键盘录入，大按钮仍可点、只给提示） */
  speechReady: boolean;
  /** 插件不可用的**具体原因**（自检面板显示；空串 = 可用） */
  speechReason: string;
  recent: { id: string; text: string; intent: string; at: string; ok: boolean }[];
  wide: boolean;
  selfCheckOpen: boolean;
  backendVersion: string;
  loginOk: boolean;
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
    speechReady: true,
    speechReason: '',
    recent: [],
    wide: false,
    selfCheckOpen: false,
    backendVersion: '',
    loginOk: false,
  },

  gate: null as SpeechGate | null,

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
      });
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
    this.setData({ wide: app.globalData.screen === 'wide' });
  },

  onUnload() {
    this.gate = null;
  },

  /** 登录 + 健康检查 + 额度（三件事都失败也不阻塞录入，只提示）。 */
  async bootstrap(): Promise<void> {
    // 不清掉「语音未启用」这句：它由 onLoad 按插件可用性设定，清掉就等于骗用户「能说」
    this.setData({ loading: true, errorText: this.data.speechReady ? '' : SPEECH_UNAVAILABLE_HINT });
    try {
      await this.doLogin();
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
      // 隐私被拒 → 把原因与修法写进自检面板，用户和开发者都看得见
      this.setData({
        speechReady: false,
        speechReason: speechReasonText(
          outcome.reason === 'privacy_denied' ? 'privacy_denied' : 'privacy_not_declared',
        ),
        errorText: outcome.hint,
      });
    }
  },

  /** 大按钮：结束说话 → 端侧已转成文字 → 建会话 → 只把文字发给后端解析。 */
  async onStop(): Promise<void> {
    if (this.gate === null) {
      return;
    }
    const said = await this.gate.stop();
    if (said === null) {
      return; // 被门禁丢弃（太短/没听到/插件没给文字），不发请求、不扣次
    }
    // 60 秒内说过同一句 → **先问再发**，别静默吞掉。
    // 此时还没建会话、还没扣额度：用户点「不记了」是零代价的。
    if (wasSaidRecently(said.text) && !(await this.confirmRealRepeat())) {
      this.setData({ talkingText: '' });
      return;
    }
    // 确认要发了才记进去（用户取消的那次不占窗口）
    rememberSpokenText(said.text);
    this.setData({ loading: true, talkingText: '识别中…' });
    try {
      // ① 建会话（拿会话 id 与令牌桶额度）→ ② 解析：只传文字，音频不出手机
      const session = await createVoiceSession({
        duration_ms: said.durationMs,
        mode: this.data.mode,
      });
      const parsed = await parseSession(session.session_id, {
        asr_text: said.text,
        duration_ms: said.durationMs,
      });
      this.handleParsed(parsed);
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
      await this.refreshUsage();
    } finally {
      this.setData({ loading: false, talkingText: '' });
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

  /** 一句话处理完：出卡 → 跳卡片页；要澄清 → 跳卡片页（带澄清）；否则提示。 */
  handleParsed(parsed: ParsePayload): void {
    if (parsed.route === 'card' && parsed.card !== null && parsed.card !== undefined) {
      this.pushRecent(parsed.asr_text, parsed.parse_result?.intent ?? '交易', true);
      wx.navigateTo({ url: `/pages/card/index?cardId=${parsed.card.meta.card_id}` });
      return;
    }
    if (parsed.route === 'clarify' && parsed.clarify !== null && parsed.clarify !== undefined) {
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
    this.setData({ recent: [item, ...this.data.recent].slice(0, 5) });
  },
});
