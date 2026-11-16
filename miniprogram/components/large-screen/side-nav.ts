/**
 * 大屏左导航（电脑版微信 `resizable` 双栏的左栏）。
 *
 * 为什么单独一个组件
 * -----------------
 * 双栏 = 「左导航固定 + 右内容滚动」。窗口变窄（<768px）时整页回落单栏，
 * 导航变成顶部横向标签——**区块一个都不能少**，只是布局变了。
 */

export interface NavItem {
  key: string;
  label: string;
  /** 是否 R3 审批入口（需要 role=boss，未授权时置灰但**仍然显示**） */
  bossOnly?: boolean;
}

Component({
  properties: {
    /** 导航项 */
    items: { type: Array, value: [] as NavItem[] },
    /** 当前选中项 key */
    active: { type: String, value: '' },
    /** 当前角色是否 boss（R3 审批入口据此置灰；手机端不显示该入口） */
    isBoss: { type: Boolean, value: false },
    /** 是否窄屏（窄屏时导航横向排列） */
    narrow: { type: Boolean, value: false },
  },

  methods: {
    /** 点击导航项。 */
    onSelect(event: WechatMiniprogram.TouchEvent): void {
      const key = String(event.currentTarget.dataset['key'] ?? '');
      this.triggerEvent('select', { key });
    },
  },
});
