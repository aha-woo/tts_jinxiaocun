/**
 * 暖纸账本风格底部操作弹窗组件（替代视觉突兀的原生 wx.showActionSheet）。
 *
 * 特点：
 * - 支持标题 + 副标题（如往来单位名称 + 当前欠款金额），不再无上下文干弹出选项；
 * - 支持 `primary`（陶土橙高亮）/ `danger`（暖砖红警示）/ `default` 语义分级；
 * - 手机端贴底全宽抽屉 + 安全区留白；电脑版微信（>=768px）居中轻量浮层卡片。
 */

export interface SheetActionItem {
  key: string;
  label: string;
  desc?: string;
  tone?: 'primary' | 'danger' | 'default';
}

Component({
  properties: {
    visible: {
      type: Boolean,
      value: false,
    },
    title: {
      type: String,
      value: '',
    },
    subtitle: {
      type: String,
      value: '',
    },
    actions: {
      type: Array,
      value: [] as SheetActionItem[],
    },
    cancelText: {
      type: String,
      value: '取消',
    },
  },

  methods: {
    onMaskTap(): void {
      this.triggerEvent('close');
    },

    onCancel(): void {
      this.triggerEvent('close');
    },

    onPick(event: WechatMiniprogram.TouchEvent): void {
      const index = Number(event.currentTarget.dataset['index'] ?? -1);
      const key = String(event.currentTarget.dataset['key'] ?? '');
      this.triggerEvent('select', { index, key });
    },

    noop(): void {},
  },
});
