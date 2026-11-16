/**
 * 槽位表（卡片 ② 区）：每行**必须**带 `source` 角标（asr / default / computed / alias / pending）。
 *
 * 规则（架构 §2.3⑥ / §8.4）：
 * - 5 值 source 一律要显示角标，`asr` 也要（用户要能分清「我说的」和「系统补的」）；
 * - `computed` 行必须给出计算式，否则行尾会显示红字提醒（服务端已强校验，这里兜底展示）；
 * - `pending` 行**不得显示成确定值**，只能显示「待确认」并高亮。
 */

Component({
  properties: {
    /** 槽位行（与 services/api.ts 的 SlotRow 结构一致） */
    rows: { type: Array, value: [] as unknown[] },
    /** 窄屏是否折叠次要行（金额/数量/方向永远不折叠） */
    collapsed: { type: Boolean, value: false },
  },

  data: {
    /** source -> 角标文案（5 值全覆盖） */
    tagText: {
      asr: '你说的',
      default: '默认',
      computed: '算出来的',
      alias: '别名',
      pending: '待确认',
    } as Record<string, string>,
    /** source -> 角标样式类 */
    tagClass: {
      asr: 'tag--asr',
      default: 'tag--default',
      computed: 'tag--computed',
      alias: 'tag--alias',
      pending: 'tag--pending',
    } as Record<string, string>,
  },

  methods: {
    /** 折叠/展开（窄屏次要区块） */
    onToggle(): void {
      this.triggerEvent('toggle', { collapsed: !this.properties.collapsed });
    },
  },
});
