/**
 * 端侧语音识别（G0/G1 门禁 + 直接出文字）：**音频一个字节都不出手机**。
 *
 * 为什么改成插件转写
 * -----------------
 * 上一版是「录音 → 上传对象存储 → 后端 ASR」：音频要出手机、要传票据、要多一次往返。
 * 现在用微信官方「同声传译」插件在**手机端**直接转成文字，只把文字发给后端
 * （``POST /api/v1/voice/sessions/{id}/parse`` 的 ``asr_text`` 字段已经是现成的）。
 * 好处是：音频不出端（隐私）、少一跳（更快）、少一个上传失败点。
 *
 * 插件配置（写错这里不会报编译错，只会在真机上「一直没反应」，所以写清楚）
 * ----------------------------------------------------------------------
 * 1. ``app.json`` 必须声明插件别名 ``WechatSI``：
 *    ``"plugins": { "WechatSI": { "version": "0.3.5", "provider": "wx069ba97219f66d99" } }``；
 * 2. ``version`` **必须**与 mp 后台「设置 → 第三方服务 → 插件管理」里**实际添加的版本号**
 *    完全一致，写错会报「插件不存在」，且是**运行时**才报。
 *    ⚠️ 官方文档示例写的是 ``0.0.7``，社区教程普遍写 ``0.3.5`` —— **以自己后台
 *    「详情」页显示的版本号为准**，不要照抄任何一篇文章（含本文件的注释）。
 * 3. 插件要先用小程序 AppID（``project.config.json`` 的 ``appid``）在后台添加并等待通过，
 *    否则 ``requirePlugin`` 抛错——本文件对此**已经做了兜底**（见构造函数），
 *    并把**失败原因**记下来给自检面板看（不然用户只知道「不能用」，不知道该改哪）。
 * 4. 隐私侧另有一道门：插件内部要调 ``wx.getRecorderManager``，所以 mp 后台
 *    「用户隐私保护指引」**必须**声明「访问你的麦克风」，否则真机上按下大按钮
 *    **一点反应都没有**。见 ``services/privacy.ts``。
 *
 * ⚠️ **不要**把 ``WechatSI`` 写进 ``project.config.json`` 的 ``useCompilerPlugins``——
 * 那个字段是**编译插件**（本项目用的是 ``typescript``），与**运行时插件**（app.json 的
 * ``plugins``）是两套东西。网上有文章让人加，那是错的，加了反而会让开发者工具
 * 去找一个不存在的编译插件。
 *
 * 两个必须记住的坑
 * ---------------
 * 1. **回调是「属性赋值」不是「方法注册」**：``manager.onStop = fn`` 会触发，
 *    ``manager.onStop(fn)`` **静默不触发**（不报错、只是永远没反应）。见 {@link SpeechGate}。
 * 2. **插件不吐逐帧能量**：所以本文件的静音判定是「有没有收到过 ``onRecognize`` 中间结果」
 *    的替代口径，**不是真 VAD**；大按钮的柱子也是视觉占位（见 {@link pseudoBars}）。
 *    真实 VAD 要等后端 ASR 接线（遗留问题 P0-B）才有。
 */

import { GATE, judgeRecording, type GateState } from './audio-gate';
import { ensurePrivacyAuthorized, type PrivacyOutcome } from './privacy';

/** 一次语音转写的结果（**只有文字**，没有音频路径）。 */
export interface SpeechResult {
  /** 转写出来的文字（已 trim；不会是空串——空串在门禁里就被丢弃了） */
  text: string;
  /** 本次说话时长（毫秒；页面建会话时要用） */
  durationMs: number;
}

/**
 * 门禁状态：在 `audio-gate.ts` 的 {@link GateState} 上**只增加**一个中间结果字段。
 *
 * 用 ``extends`` 而不是另立一套，是因为大按钮组件的 ``bars`` / ``durationMs`` /
 * ``softLimit`` 三个入参就是按 `GateState` 写的——扩展**不改变**它们。
 */
export interface SpeechGateState extends GateState {
  /** 插件的中间识别结果（页面据此实时显示「正在说：…」；空串表示还没吐字） */
  partialText: string;
}

/** 状态回调（与 `AudioGate.onState` 同形）。 */
export type SpeechListener = (state: SpeechGateState) => void;

/** 端侧语音识别适配器接口（方便未来无痛切换云端 ASR 实现）。 */
export interface SpeechAdapter {
  readonly available: boolean;
  unavailableReason: SpeechUnavailableReason | '';
  onState(listener: SpeechListener): void;
  onAutoStop(listener: (result: SpeechResult | null) => void): void;
  start(): Promise<PrivacyOutcome>;
  stop(): Promise<SpeechResult | null>;
  dispose(): void;
}

/**
 * 同声传译插件的最小类型声明。
 *
 * `miniprogram-api-typings` 只声明了全局的 ``requirePlugin``（返回 ``any``），
 * 没有任何 WechatSI 的类型，所以这里自己声明一份**只包含本文件用到的成员**。
 * 注意四个回调都是**可赋值的可选属性**（不是方法）——这正是坑 1 的类型级体现。
 */
interface RecordRecognitionManager {
  /** 开始录音回调（**属性赋值**） */
  onStart?: () => void;
  /** 中间识别结果（持续吐出，用来做「正在说：…」） */
  onRecognize?: (res: { result?: string }) => void;
  /** 结束回调：``res.result`` 是最终文字（**属性赋值**） */
  onStop?: (res: { result?: string }) => void;
  /**
   * 出错回调（**属性赋值**）。
   *
   * 注意：同声传译插件的错误对象字段名**不是统一叫 errMsg**——社区与实测里见过
   * ``errMsg`` / ``msg`` / ``retmsg`` 三种写法，还带 ``retcode``。只读 ``errMsg``
   * 会把真实原因吞成「未知原因」（2026-11-19 开发者工具里就踩到了）。
   */
  onError?: (res: { errMsg?: string; msg?: string; retmsg?: string; retcode?: number }) => void;
  start: (options: { lang: string; duration: number }) => void;
  stop: () => void;
}

/** 插件对象（只用到这一个工厂方法）。 */
interface WechatSiPlugin {
  getRecordRecognitionManager: () => RecordRecognitionManager;
}

/** 插件不可用时的统一提示（页面与大按钮共用同一句话，避免两处文案不一致）。 */
export const SPEECH_UNAVAILABLE_HINT = '语音识别未启用（插件未配置好），可以先用「手动记账」';

/**
 * 插件不可用的**具体原因**（给自检面板看，别让用户去猜）。
 *
 * 为什么要有这个
 * --------------
 * 「点大按钮没反应」有五种完全不同的原因（版本号写错 / 后台没添加 / 后台没通过 /
 * 隐私没声明麦克风 / 基础库过低），而它们的**修法在完全不同的地方**。
 * 只给一句「未启用」，用户和开发者都无从下手——只能反复试。
 * 所以这里把原因码原样暴露出去，自检面板逐条显示。
 */
export type SpeechUnavailableReason =
  | 'plugin_load_failed'
  | 'privacy_denied'
  | 'privacy_not_declared'
  | 'not_supported';

/** 把不可用原因翻成人话（自检面板直接显示）。 */
export function speechReasonText(reason: SpeechUnavailableReason | ''): string {
  switch (reason) {
    case 'plugin_load_failed':
      return '插件加载失败（检查 app.json 的 version 是否与后台「详情」页一致）';
    case 'privacy_denied':
      return '用户未同意隐私协议（需后台声明「访问你的麦克风」）';
    case 'privacy_not_declared':
      return '后台未声明「访问你的麦克风」（设置 → 服务内容声明 → 用户隐私保护指引）';
    case 'not_supported':
      return '当前微信版本不支持语音插件（需升级微信）';
    default:
      return '未知原因';
  }
}

/**
 * 生成 4~7 根柱子的节律序列（**视觉占位，不是真实能量**）。
 *
 * 插件不提供逐帧能量，所以这里用「已经说了多久 + 有没有收到过中间结果」合成一个
 * 确定性的起伏序列：同样的毫秒数永远得到同样的柱高（可复现、可单测），
 * 且不会像随机数那样闪得让人头晕。**它不是 VAD，不参与任何判定。**
 *
 * @param durationMs 已录时长（毫秒）
 * @param gotSpeech 是否已经收到过中间识别结果
 * @returns 每根柱子的高度（12~100）
 */
export function pseudoBars(durationMs: number, gotSpeech: boolean): number[] {
  // 4 根起步，每说满 2 秒多一根，最多 7 根
  const count = 4 + Math.min(3, Math.floor(Math.max(0, durationMs) / 2000));
  // 没说上话时振幅压到 0.4：用户一眼能看出「它还没听到内容」
  const amplitude = gotSpeech ? 1 : 0.4;
  const bars: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const phase = durationMs / 320 + index * 0.9;
    const wave = (Math.sin(phase) + 1) / 2;
    const height = Math.round(30 + wave * 60 * amplitude);
    bars.push(Math.max(12, Math.min(100, height)));
  }
  return bars;
}

/**
 * 语音门禁（封装同声传译插件，对外只暴露「开始/停止 + 状态回调」）。
 *
 * 与 `AudioGate` **同形**，所以页面换类时其余逻辑不用动：
 * ``onState`` / ``start`` / ``stop`` / ``isRecording``，额外多一个 {@link available}。
 *
 * 插件缺失时**绝不抛错、绝不白屏**：``available === false``，页面据此降级成
 * 「语音未启用、走键盘」，点了也只给一句提示。
 */
export class SpeechGate implements SpeechAdapter {
  /** 插件是否可用（构造期一次性判定；页面用它决定是否显示降级提示） */
  readonly available: boolean;

  /** 不可用的具体原因（``available === true`` 时是空串） */
  unavailableReason: SpeechUnavailableReason | '';

  /** 最近一次隐私授权的结果（页面可据此提示用户去哪儿开） */
  privacy: PrivacyOutcome = { granted: true };

  private manager: RecordRecognitionManager | null = null;

  private listener: SpeechListener | null = null;

  private startedAt = 0;

  private timer: number | null = null;

  private recording = false;

  /** 是否收到过中间识别结果（**静音判定的替代口径**，不是 VAD） */
  private gotSpeech = false;

  private partialText = '';

  private resolver: ((result: SpeechResult | null) => void) | null = null;

  private autoStopListener: ((result: SpeechResult | null) => void) | null = null;

  private autoStopping = false;

  private stopTimeoutTimer: number | null = null;

  /**
   * 正在进行的启动流程（隐私授权 → 开始录音）。
   *
   * 存在的原因：「按住说」模式下，用户可能在隐私弹窗还没点完时就抬手了。
   * {@link stop} 靠它判断「要不要等启动完成再停」，避免把用户已经说出口的话丢掉。
   */
  private startPromise: Promise<PrivacyOutcome> | null = null;

  constructor() {
    let manager: RecordRecognitionManager | null = null;
    let reason: SpeechUnavailableReason | '' = 'not_supported';
    try {
      // requirePlugin 在「后台没添加插件 / 版本号写错 / 基础库过低」时都会抛
      const plugin = requirePlugin('WechatSI') as WechatSiPlugin;
      manager = plugin.getRecordRecognitionManager();
    } catch (error) {
      // 这里**不能抛**：抛出去就是首页白屏，用户连「手动记账」都点不到
      console.warn('[speech] 同声传译插件不可用，降级为键盘录入', error);
      manager = null;
      reason = 'plugin_load_failed';
    }
    this.manager = manager;
    this.available = manager !== null;
    this.unavailableReason = manager === null ? reason : '';
    if (manager !== null) {
      this.bind(manager);
    }
  }

  /** 绑定状态回调（页面 onLoad 里调一次）；与 `AudioGate.onState` 同形。 */
  onState(listener: SpeechListener): void {
    this.listener = listener;
  }

  /** 绑定录音到达时长上限自动停止的结果回调（防止 20s 录音静默丢失）。 */
  onAutoStop(listener: (result: SpeechResult | null) => void): void {
    this.autoStopListener = listener;
  }

  /** 页面卸载时清理定时器和回调，避免内存泄漏。 */
  dispose(): void {
    this.clearTimer();
    this.clearStopTimeout();
    this.listener = null;
    this.autoStopListener = null;
    if (this.recording && this.manager !== null) {
      this.recording = false;
      try {
        this.manager.stop();
      } catch (_error) {
        // ignore
      }
    }
  }

  /** 是否正在说话（录音）。 */
  get isRecording(): boolean {
    return this.recording;
  }

  /**
   * 开始说话。
   *
   * 顺序是刻意的：**先过隐私授权，再调插件**。
   * 反过来的话，用户在没授权的情况下按下按钮 → 插件内部调
   * ``wx.getRecorderManager`` 报 errno 112 → 表现为「按了没反应」，
   * 而用户完全不知道要去哪儿开麦克风。现在是**先拦下来、明确告诉他**。
   *
   * @returns 隐私授权结果；``granted=false`` 时**没有开始录音**
   */
  async start(): Promise<PrivacyOutcome> {
    // 已在录音中 → 忽略（重复按）
    if (this.recording) {
      return { granted: true };
    }
    const manager = this.manager;
    if (manager === null) {
      wx.showToast({ title: SPEECH_UNAVAILABLE_HINT, icon: 'none' });
      return { granted: false, reason: this.unavailableReason, hint: SPEECH_UNAVAILABLE_HINT };
    }

    // 先登记 promise：**在 await 之前**登记，否则「按下后立刻抬手」会漏掉这次启动
    const pending = this.beginStart(manager);
    this.startPromise = pending;
    return pending;
  }

  /** 真正的启动流程（隐私授权 → 开始录音）。 */
  private async beginStart(manager: RecordRecognitionManager): Promise<PrivacyOutcome> {
    // 隐私协议：后台没声明麦克风 → 官方隐私弹窗会拦；用户拒绝 → 明确告知
    const privacy = await ensurePrivacyAuthorized();
    this.privacy = privacy;
    if (!privacy.granted) {
      wx.showToast({ title: privacy.hint, icon: 'none' });
      this.startPromise = null;
      return privacy;
    }

    this.gotSpeech = false;
    this.partialText = '';
    this.startedAt = Date.now();
    this.recording = true;
    manager.start({ lang: 'zh_CN', duration: GATE.hardMaxMs });
    this.timer = setInterval(() => this.tick(), 100) as unknown as number;
    this.emit();
    this.startPromise = null;
    return { granted: true };
  }

  /**
   * 停止并取文字；返回 `null` 表示被门禁丢弃（**不发请求、不扣次**）。
   *
   * ⚠️ 「按住说」模式下的**竞态**（必须处理，否则用户会觉得「我说了半天没反应」）
   * -------------------------------------------------------------------
   * 按下 → 触发隐私授权（**异步**，可能要弹窗、要等用户点） → 抬手 → 触发 stop。
   * 如果用户在授权完成**之前**就抬手了，`recording` 还是 false，
   * 这时直接返回 ``null`` 就等于**把用户已经说出口的那句话丢了**。
   *
   * 所以这里记下 {@link startPromise}：stop 时若还在启动中，就**等它启动完再停**。
   * （隐私被拒 → 根本没开始录 → 返回 null，这个是对的。）
   */
  stop(): Promise<SpeechResult | null> {
    const manager = this.manager;
    if (manager === null) {
      return Promise.resolve(null);
    }
    const pending = this.startPromise;
    if (pending !== null) {
      // 启动还没走完 → 等它走完，再决定要不要停
      return pending.then((outcome) => {
        if (!outcome.granted) {
          return null; // 隐私被拒：压根没开始录
        }
        return this.stopNow(manager);
      });
    }
    if (!this.recording) {
      return Promise.resolve(null);
    }
    return this.stopNow(manager);
  }

  /** 真正执行停止（{@link stop} 处理好竞态后调用）。 */
  private stopNow(manager: RecordRecognitionManager): Promise<SpeechResult | null> {
    if (!this.recording) {
      return Promise.resolve(null);
    }
    return new Promise<SpeechResult | null>((resolve) => {
      this.resolver = resolve;
      this.clearStopTimeout();
      this.stopTimeoutTimer = setTimeout(() => {
        if (this.resolver !== null) {
          const cb = this.resolver;
          this.resolver = null;
          this.recording = false;
          this.clearTimer();
          this.emit();
          wx.showToast({ title: '语音服务响应超时，请重试', icon: 'none' });
          cb(null);
        }
      }, 4000) as unknown as number;
      manager.stop();
    });
  }

  // ------------------------------------------------------------------ 内部
  private bind(manager: RecordRecognitionManager): void {
    // ⚠️ 坑 1：**属性赋值**，不是 `manager.onStop(fn)`。
    // 写成方法调用不会报错，只是这些回调永远不触发（最典型的「点了没反应」）。
    manager.onStart = () => {
      this.emit();
    };

    manager.onRecognize = (res: { result?: string }) => {
      const partial = (res.result ?? '').trim();
      if (partial !== '') {
        this.gotSpeech = true;
        this.partialText = partial;
      }
      this.emit();
    };

    manager.onStop = (res: { result?: string }) => {
      this.recording = false;
      this.clearTimer();
      this.clearStopTimeout();
      const durationMs = Math.max(0, Date.now() - this.startedAt);
      const text = (res.result ?? '').trim();
      const wasAuto = this.autoStopping;
      this.autoStopping = false;
      this.emit();

      const resolve = this.resolver;
      this.resolver = null;
      const deliver = (out: SpeechResult | null): void => {
        if (resolve !== null) {
          resolve(out);
        } else if (wasAuto && this.autoStopListener !== null) {
          this.autoStopListener(out);
        }
      };

      // 门禁**复用** `audio-gate.ts` 的 judgeRecording，不另立一套阈值。
      // 插件不吐逐帧能量，所以用「有没有听到过内容」合成一个等价能量序列：
      // 听到了 → `[1]`（不算静音）；完全没听到 → `[]`（judgeRecording 判 silent）。
      const heard = this.gotSpeech || text !== '';
      const verdict = judgeRecording(durationMs, heard ? [1] : []);
      if (!verdict.ok) {
        wx.showToast({ title: verdict.message, icon: 'none' });
        deliver(null);
        return;
      }
      if (text === '') {
        // 门禁过了（时长够、也听到过动静），但插件最后没给文字：按「没听清」处理
        wx.showToast({ title: '没听清，再说一遍', icon: 'none' });
        deliver(null);
        return;
      }
      deliver({ text, durationMs });
    };

    manager.onError = (res: { errMsg?: string; msg?: string; retmsg?: string; retcode?: number }) => {
      this.recording = false;
      this.autoStopping = false;
      this.clearTimer();
      this.clearStopTimeout();
      this.emit();
      // 插件错误对象的字段名不统一（errMsg / msg / retmsg），且常常只带 retcode。
      // 原样打进 console 便于排查；toast 只显示短文案（太长会被截断）。
      const detail = res.errMsg ?? res.msg ?? res.retmsg ?? '';
      const code = res.retcode !== undefined ? `retcode=${res.retcode}` : '';
      console.error('[speech] 同声传译 onError', res);
      const reason = detail || code || '未知原因';
      wx.showToast({ title: `语音识别失败：${reason}`.slice(0, 40), icon: 'none' });
      const resolve = this.resolver;
      this.resolver = null;
      if (resolve !== null) {
        resolve(null);
      }
    };
  }

  /** 每 100ms 刷一次状态；到硬上限自己停（与 `AudioGate.tick()` 同一逻辑）。 */
  private tick(): void {
    if (Date.now() - this.startedAt >= GATE.hardMaxMs) {
      this.autoStopping = true;
      this.clearTimer();
      this.manager?.stop();
      return;
    }
    this.emit();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private clearStopTimeout(): void {
    if (this.stopTimeoutTimer !== null) {
      clearTimeout(this.stopTimeoutTimer);
      this.stopTimeoutTimer = null;
    }
  }

  private emit(): void {
    if (this.listener === null) {
      return;
    }
    const durationMs = this.recording ? Date.now() - this.startedAt : 0;
    this.listener({
      recording: this.recording,
      durationMs,
      bars: this.recording ? pseudoBars(durationMs, this.gotSpeech) : [],
      softLimitReached: durationMs >= GATE.softMaxMs,
      partialText: this.partialText,
    });
  }
}
