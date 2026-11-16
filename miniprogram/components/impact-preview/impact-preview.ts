/**
 * 影响预览（卡片 ④ 区）：把「这一笔会让数变成什么」摊开给用户看。
 *
 * 三条要点（T04 `impact_preview` 的端侧呈现）：
 * - 只读预演的结果，**不是**「已经写进去了」；
 * - 每一行显示 `before → after`，变化为负/超额时高亮；
 * - 显示 `preview_version`：落账时服务端用它做 CAS，版本变了会返回
 *   `E_PREVIEW_VERSION_CONFLICT` 让用户重新预演（端上要能解释这句）。
 */

Component({
  properties: {
    /** 影响预览行 */
    rows: { type: Array, value: [] as unknown[] },
    /** 预演时的数据版本（CAS 的期望值） */
    previewVersion: { type: Number, value: 0 },
    /** 服务端附注（例如「跨门店不计入」） */
    note: { type: String, value: '' },
  },

  methods: {
    /** 用户点「重新预演」（版本冲突时引导） */
    onRefresh(): void {
      this.triggerEvent('refresh');
    },
  },
});
