/**
 * 数字键盘 + 常用商品点选：**让一次完整记账全程脱离语音**（T05 验收②）。
 *
 * 为什么这不是可选功能
 * -------------------
 * 面向的是「电脑不熟练的商家」，以及在嘈杂环境/嗓子不舒服时打字更快的场景。
 * 所以它必须能独立走完「选商品 → 填数量 → 填单价 → 选方向 → 提交」，
 * **一次语音都不用**——而不是「语音失败后的降级提示页」。
 */

import { appendNumericKey } from '../../utils/format';

Component({
  properties: {
    /** 当前输入的数量文本（受控） */
    quantity: { type: String, value: '' },
    /** 当前输入的单价格文本（元，提交时转成分） */
    price: { type: String, value: '' },
    /** 常用商品候选（来自服务端 TopN，点击即选中） */
    products: { type: Array, value: [] as string[] },
    /** 选中的商品名 */
    product: { type: String, value: '' },
    /** 方向：in = 进货，out = 卖货 */
    direction: { type: String, value: 'in' },
    /** 是否展示方向切换（由宿主页面自绘时可关闭） */
    showDirection: { type: Boolean, value: true },
    /** 是否展示常用商品 chips（由宿主页面统一选货区接管时可关闭） */
    showProducts: { type: Boolean, value: true },
  },

  data: {
    /** 统一 3×4 按键布局：支持小数点与 SVG 退格 */
    keys: ['1', '2', '3', '4', '5', '6', '7', '8', '9', '.', '0', 'del'] as string[],
    /** 当前输入焦点：quantity / price */
    focus: 'quantity' as 'quantity' | 'price',
  },

  methods: {
    /** 切换输入焦点。 */
    onFocus(event: WechatMiniprogram.TouchEvent): void {
      const focus = String(event.currentTarget.dataset['focus'] ?? 'quantity');
      this.setData({ focus: focus === 'price' ? 'price' : 'quantity' });
    },

    /** 点商品候选 → 选中。 */
    onPickProduct(event: WechatMiniprogram.TouchEvent): void {
      const name = String(event.currentTarget.dataset['name'] ?? '');
      this.triggerEvent('product', { product: name });
    },

    /** 切换方向（进货 / 卖货）。 */
    onPickDirection(event: WechatMiniprogram.TouchEvent): void {
      const direction = String(event.currentTarget.dataset['direction'] ?? 'in');
      this.triggerEvent('direction', { direction });
    },

    /** 点键盘：统一数字/小数点/退格校验。 */
    onKey(event: WechatMiniprogram.TouchEvent): void {
      const key = String(event.currentTarget.dataset['key'] ?? '');
      const field = this.data.focus === 'price' ? 'price' : 'quantity';
      const current = String((this.data.focus === 'price' ? this.properties.price : this.properties.quantity) ?? '');
      const next = appendNumericKey(current, key, 8);
      this.triggerEvent('input', { field, value: next });
    },

    /** 提交（页面对必填项做最终校验）。 */
    onSubmit(): void {
      this.triggerEvent('submit');
    },
  },
});
