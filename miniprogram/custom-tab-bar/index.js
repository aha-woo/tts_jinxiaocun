Component({
  data: {
    selected: 0,
    left: [
      { index: 0, pagePath: '/pages/index/index', text: '今天' },
      { index: 1, pagePath: '/pages/goods/index', text: '货品' },
    ],
    right: [
      { index: 2, pagePath: '/pages/debts/index', text: '欠款' },
      { index: 3, pagePath: '/pages/mine/index', text: '我的' },
    ],
  },

  methods: {
    onTab(event) {
      const index = Number(event.currentTarget.dataset.index);
      const all = this.data.left.concat(this.data.right);
      const item = all.find((row) => row.index === index);
      if (item === undefined || index === this.data.selected) {
        return;
      }
      wx.switchTab({ url: item.pagePath });
    },

    // 中间说话键：已在今天页时直接触发录音；在其他页切回今天页再触发
    onMic() {
      const pages = getCurrentPages();
      const top = pages[pages.length - 1];
      if (top && top.route === 'pages/index/index' && typeof top.onVoiceTap === 'function') {
        void top.onVoiceTap();
        return;
      }
      const app = getApp();
      app.globalData.voiceRequested = true;
      wx.switchTab({ url: '/pages/index/index' });
    },
  },
});
