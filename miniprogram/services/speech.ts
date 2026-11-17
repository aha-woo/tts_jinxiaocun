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
 *    完全一致，写错会报「插件不存在」，且是**运行时**才报；
 * 3. 插件要先用小程序 AppID（``project.config.json`` 的 ``appid``）在后台添加并等待通过，
 *    否则 ``requirePlugin`` 抛错——本文件对此**已经做了兜底**（见构造函数）。
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
  /** 出错回调（**属性赋值**） */
  onError?: (res: { errMsg?: string; retcode?: number }) => void;
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
export class SpeechGate {
  /** 插件是否可用（构造期一次性判定；页面用它决定是否显示降级提示） */
  readonly available: boolean;

  private manager: RecordRecognitionManager | null = null;

  private listener: SpeechListener | null = null;

  private startedAt = 0;

  private timer: number | null = null;

  private recording = false;

  /** 是否收到过中间识别结果（**静音判定的替代口径**，不是 VAD） */
  private gotSpeech = false;

  private partialText = '';

  private resolver: ((result: SpeechResult | null) => void) | null = null;

  constructor() {
    let manager: RecordRecognitionManager | null = null;
    try {
      // requirePlugin 在「后台没添加插件 / 版本号写错 / 基础库过低」时都会抛
      const plugin = requirePlugin('WechatSI') as WechatSiPlugin;
      manager = plugin.getRecordRecognitionManager();
    } catch (error) {
      // 这里**不能抛**：抛出去就是首页白屏，用户连「手动记账」都点不到
      console.warn('[speech] 同声传译插件不可用，降级为键盘录入', error);
      manager = null;
    }
    this.manager = manager;
    this.available = manager !== null;
    if (manager !== null) {
      this.bind(manager);
    }
  }

  /** 绑定状态回调（页面 onLoad 里调一次）；与 `AudioGate.onState` 同形。 */
  onState(listener: SpeechListener): void {
    this.listener = listener;
  }

  /** 是否正在说话（录音）。 */
  get isRecording(): boolean {
    return this.recording;
  }

  /** 开始说话（已在说话中则忽略）。 */
  start(): void {
    const manager = this.manager;
    if (this.recording) {
      return;
    }
    if (manager === null) {
      wx.showToast({ title: SPEECH_UNAVAILABLE_HINT, icon: 'none' });
      return;
    }
    this.gotSpeech = false;
    this.partialText = '';
    this.startedAt = Date.now();
    this.recording = true;
    manager.start({ lang: 'zh_CN', duration: GATE.hardMaxMs });
    this.timer = setInterval(() => this.tick(), 100) as unknown as number;
    this.emit();
  }

  /** 停止并取文字；返回 `null` 表示被门禁丢弃（**不发请求、不扣次**）。 */
  stop(): Promise<SpeechResult | null> {
    const manager = this.manager;
    if (!this.recording || manager === null) {
      return Promise.resolve(null);
    }
    return new Promise<SpeechResult | null>((resolve) => {
      this.resolver = resolve;
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
      const durationMs = Math.max(0, Date.now() - this.startedAt);
      const text = (res.result ?? '').trim();
      this.emit();

      const resolve = this.resolver;
      this.resolver = null;
      if (resolve === null) {
        return;
      }

      // 门禁**复用** `audio-gate.ts` 的 judgeRecording，不另立一套阈值。
      // 插件不吐逐帧能量，所以用「有没有听到过内容」合成一个等价能量序列：
      // 听到了 → `[1]`（不算静音）；完全没听到 → `[]`（judgeRecording 判 silent）。
      const heard = this.gotSpeech || text !== '';
      const verdict = judgeRecording(durationMs, heard ? [1] : []);
      if (!verdict.ok) {
        wx.showToast({ title: verdict.message, icon: 'none' });
        resolve(null);
        return;
      }
      if (text === '') {
        // 门禁过了（时长够、也听到过动静），但插件最后没给文字：按「没听清」处理
        wx.showToast({ title: '没听清，再说一遍', icon: 'none' });
        resolve(null);
        return;
      }
      resolve({ text, durationMs });
    };

    manager.onError = (res: { errMsg?: string }) => {
      this.recording = false;
      this.clearTimer();
      this.emit();
      wx.showToast({ title: `语音识别失败：${res.errMsg ?? '未知原因'}`, icon: 'none' });
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
      void this.stop();
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
