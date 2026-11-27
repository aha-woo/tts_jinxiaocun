import { appendNumericKey } from '../../utils/format';

function buildKeys(allowDecimal: boolean): string[] {
  return ['1', '2', '3', '4', '5', '6', '7', '8', '9', allowDecimal ? '.' : 'clear', '0', 'del'];
}

Component({
  properties: {
    visible: { type: Boolean, value: false },
    title: { type: String, value: '' },
    subtitle: { type: String, value: '' },
    value: { type: String, value: '' },
    unit: { type: String, value: '' },
    confirmText: { type: String, value: '确定' },
    allowDecimal: { type: Boolean, value: true },
    maxLen: { type: Number, value: 8 },
  },

  data: {
    keys: buildKeys(true),
  },

  observers: {
    allowDecimal(allow: boolean) {
      this.setData({ keys: buildKeys(allow !== false) });
    },
  },

  methods: {
    onKey(event: WechatMiniprogram.TouchEvent): void {
      const key = String(event.currentTarget.dataset['key'] ?? '');
      if (key === 'clear') {
        this.triggerEvent('change', { value: '' });
        return;
      }
      const current = String(this.properties.value ?? '');
      const maxLen = Number(this.properties.maxLen) > 0 ? Number(this.properties.maxLen) : 8;
      const next = appendNumericKey(current, key, maxLen);
      this.triggerEvent('change', { value: next });
    },

    onConfirm(): void {
      this.triggerEvent('confirm', { value: String(this.properties.value ?? '') });
    },

    onClose(): void {
      this.triggerEvent('close');
    },

    noop(): void {},
  },
});
