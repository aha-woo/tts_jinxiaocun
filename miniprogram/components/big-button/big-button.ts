/**
 * 语音大按钮：**拇指热区 ≥ 88×88px**（PRD 3.2【假设，需核实】），
 * 支持「按住说」与「点一下说」两种模式，录音中显示时长 + 能量柱。
 *
 * 组件只负责**采集手势与展示状态**，录音与门禁在 `services/audio-gate.ts`，
 * 这样「阈值改一处」与「UI 改一处」互不影响。
 */

Component({
  properties: {
    /** 是否正在录音（由页面持有的 AudioGate 驱动） */
    recording: { type: Boolean, value: false },
    /** 录音时长（毫秒） */
    durationMs: { type: Number, value: 0 },
    /** 能量柱高度百分比数组（10~100） */
    bars: { type: Array, value: [] as number[] },
    /** 是否已到软上限（15s），提示「快说完了」 */
    softLimit: { type: Boolean, value: false },
    /** 是否禁用（如降级到 L4 只读） */
    disabled: { type: Boolean, value: false },
    /** 模式：hold = 按住说，tap = 点一下说 */
    mode: { type: String, value: 'hold' },
  },

  data: {
    /** 展示用时长文本 */
    durationText: '0.0s',
  },

  observers: {
    durationMs(value: number) {
      this.setData({ durationText: `${(value / 1000).toFixed(1)}s` });
    },
  },

  methods: {
    /** 按住说：手指按下开始录音。 */
    onTouchStart(): void {
      if (this.data.disabled || this.data.mode !== 'hold') {
        return;
      }
      this.triggerEvent('start');
    },

    /** 按住说：抬手结束录音。 */
    onTouchEnd(): void {
      if (this.data.disabled || this.data.mode !== 'hold') {
        return;
      }
      this.triggerEvent('stop');
    },

    /** 点一下说：单击切换开始/结束。 */
    onTap(): void {
      if (this.data.disabled || this.data.mode !== 'tap') {
        return;
      }
      this.triggerEvent(this.data.recording ? 'stop' : 'start');
    },

    /** 切换触发模式（按住说 / 点一下说）。 */
    onSwitchMode(): void {
      this.triggerEvent('modechange', { mode: this.data.mode === 'hold' ? 'tap' : 'hold' });
    },
  },
});
