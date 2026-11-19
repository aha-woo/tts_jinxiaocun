/**
 * 确认卡片页：**任何写操作都必须经过这里**。
 *
 * 7 区块（服务端 `ConfirmCardView.to_payload()`，一个都不能少）
 * ----------------------------------------------------------
 * ① `original` 原文 ② `intent` 意图 ③ `slots` 槽位（带 source 角标）
 * ④ `calc` 计算 ⑤ `impact` 影响预览 ⑥ `risk` 风险 ⑦ `actions` 按钮
 *
 * 确认与超时
 * ---------
 * - C1：点主按钮一次即可；
 * - C2：**二次语音**确认（`confirm2`），同时永远提供**按钮兜底**；
 * - **任何情况下禁止超时自动执行**：30s 到点只提示，卡片进「待确认」，
 *   不落账、不重复扣次（复活复用同一张卡）。
 */

import {
  ApiError,
  answerClarify,
  cancelCard,
  confirm2,
  confirmCard,
  getCard,
  listPending,
  type CardPayload,
  type ClarifyAnswerPayload,
  type ClarifyPayload,
} from '../../services/api';

/** 卡片 30 秒有效期（与服务端 `card_ttl_seconds` 一致；端上只倒计时提示）。 */
export const CARD_TTL_SEC = 30;

/** 方向标识（首屏必须看见「这是进货还是卖货」）。 */
export interface DirectionBadge {
  /** 展示文案：进货 / 卖货 / 空串（非交易类意图） */
  text: string;
  /** 色调：in=进货（绿）/ out=卖货（蓝）/ none */
  tone: 'in' | 'out' | 'none';
}

/**
 * 从意图 id 推出方向标识。
 *
 * 服务端意图命名已按方向分组（`purchase.*` / `sales.*`），所以这里**不需要**再读文本，
 * 只做前缀映射——0 成本、0 歧义。非交易类（查询 / 采购退货等）返回空串，不显示角标。
 */
export function directionOfIntent(intentId: string): DirectionBadge {
  if (intentId.startsWith('purchase.')) {
    return { text: '进货', tone: 'in' };
  }
  if (intentId.startsWith('sales.')) {
    return { text: '卖货', tone: 'out' };
  }
  return { text: '', tone: 'none' };
}

/** 分 -> `¥12.34`（端上只做展示，**不参与任何计算**）。 */
export function fenToYuanText(fen: number): string {
  const safe = Number.isFinite(fen) ? fen : 0;
  return `¥${(safe / 100).toFixed(2)}`;
}

/**
 * 组装面向用户的「基本信息」。
 *
 * 只暴露日期/付款/供应商/仓库四项，其余槽位（Q/U/P/UP/TA…）属于解析内部字段，
 * 不进用户界面。**值为空或来源是 pending 的一律不显示**——宁可不写，也不让
 * 用户看到「待确认」这种内部状态当成事实。
 */
export function buildInfoFields(card: CardPayload): { label: string; value: string }[] {
  const rows = card.slots ?? [];
  const result: { label: string; value: string }[] = [];
  for (const field of INFO_FIELDS) {
    const row = rows.find((item) => item.code === field.code);
    if (row === undefined) {
      continue;
    }
    if (row.source === 'pending') {
      continue;
    }
    let value = String(row.display ?? '').trim();
    if (field.code === 'PS') {
      value = PAYMENT_TEXT[value] ?? value;
    }
    if (value === '' || value === '待确认') {
      continue;
    }
    result.push({ label: field.label, value });
  }
  return result;
}

/**
 * 商品还没有档案（不能直接确认，必须先建商品）。
 *
 * 判据三条取并集，因为不同意图的载荷落点不同：
 * 明细行 ``pending`` / ``P`` 槽位来源是 ``pending`` / 计算式里写了「未在商品档案」。
 */
export function detectPendingProduct(card: CardPayload): boolean {
  if ((card.calc.lines ?? []).some((line) => line.pending)) {
    return true;
  }
  if ((card.slots ?? []).some((slot) => slot.code === 'P' && slot.source === 'pending')) {
    return true;
  }
  return (card.calc.calc_traces ?? []).some((trace) => trace.includes('未在商品档案'));
}

interface CardData {
  loading: boolean;
  cardId: string;
  sessionId: string;
  card: CardPayload | null;
  clarify: ClarifyPayload | null;
  countdown: number;
  expired: boolean;
  needSecondVoice: boolean;
  secondVoiceDone: boolean;
  errorText: string;
  wide: boolean;
  riskTone: string;
  /** 首屏方向标识文案 */
  directionText: string;
  /** 首屏总金额文案（`¥70.00`） */
  totalAmountText: string;
  /** 卡片标题（进货单 / 卖货单 / 意图中文名） */
  cardTitle: string;
  /** 面向用户的「基本信息」（日期/付款/供应商/仓库，空值自动隐藏） */
  infoFields: { label: string; value: string }[];
  /** 商品还没有档案：不能直接确认，要先建商品 */
  hasPendingProduct: boolean;
  /** 是否展开「解析明细（开发用）」——默认折叠 */
  devDetail: boolean;
}

/** 只把这四个槽位展示给用户；其余是内部字段。 */
const INFO_FIELDS: { code: string; label: string }[] = [
  { code: 'D', label: '日期' },
  { code: 'PS', label: '付款' },
  { code: 'S', label: '供应商' },
  { code: 'W', label: '仓库' },
];

/** 付款状态的人话映射（服务端存的是「已结/未结/部分」）。 */
const PAYMENT_TEXT: Record<string, string> = {
  已结: '已付清',
  未结: '还欠着',
  部分: '付了一部分',
};

Page<CardData, WechatMiniprogram.Page.CustomOption>({
  data: {
    loading: true,
    cardId: '',
    sessionId: '',
    card: null,
    clarify: null,
    countdown: CARD_TTL_SEC,
    expired: false,
    needSecondVoice: false,
    secondVoiceDone: false,
    errorText: '',
    wide: false,
    riskTone: 'ok',
    directionText: '',
    totalAmountText: '¥0.00',
    cardTitle: '',
    infoFields: [],
    hasPendingProduct: false,
    devDetail: false,
  },

  timer: null as number | null,

  onLoad(query: Record<string, string | undefined>) {
    const cardId = String(query['cardId'] ?? '');
    const sessionId = String(query['sessionId'] ?? '');
    this.setData({ cardId, sessionId });
    const app = getApp<{ globalData: { screen: 'phone' | 'wide' } }>();
    this.setData({ wide: app.globalData.screen === 'wide' });
    if (cardId !== '') {
      void this.loadCard(cardId);
    } else {
      // 从会话进来但还没有卡片：说明还在澄清阶段
      this.setData({ loading: false, errorText: '还差一项没确认，回答下面的问题就会生成卡片' });
    }
  },

  onUnload() {
    this.stopTimer();
  },

  /** 拉卡片（7 区块）。 */
  async loadCard(cardId: string): Promise<void> {
    this.setData({ loading: true, errorText: '' });
    try {
      const card = await getCard(cardId);
      const direction = directionOfIntent(card.intent.intent_id);
      const totalFen = card.calc.lines.reduce((sum, line) => {
        const value = typeof line.amount_fen === 'number' ? line.amount_fen : 0;
        return sum + value;
      }, 0);
      this.setData({
        card,
        directionText: direction.text === '' ? card.intent.intent_label_zh : direction.text,
        cardTitle:
          direction.text === ''
            ? card.intent.intent_label_zh
            : `${direction.text}单`,
        totalAmountText: fenToYuanText(totalFen),
        infoFields: buildInfoFields(card),
        hasPendingProduct: detectPendingProduct(card),
        needSecondVoice: card.intent.confirm_policy === 'C2',
        riskTone: card.risk.level === 'ok' ? 'ok' : 'warn',
        loading: false,
      });
      this.startTimer();
    } catch (error) {
      this.setData({ loading: false, errorText: this.messageOf(error) });
    }
  },

  /**
   * 澄清答完后把卡片找回来。
   *
   * 点选接口只返回结构化槽位（不返回卡片），所以这里按 sessionId 从「待确认」里
   * 找到同一张卡再跳过去——**复用同一张卡，不重复扣次、不重调 AI**。
   */
  async resolveCardFromSession(): Promise<void> {
    try {
      const pending = await listPending();
      const hit = (pending.items ?? []).find(
        (item) => item.meta.session_id === this.data.sessionId
      );
      if (hit === undefined) {
        this.setData({ errorText: '澄清好了但没找到卡片，请回首页再来一次' });
        return;
      }
      wx.redirectTo({ url: `/pages/card/index?cardId=${hit.meta.card_id}` });
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** 30s 倒计时：**只更新 UI，到点绝不自动提交**。 */
  startTimer(): void {
    this.stopTimer();
    this.timer = setInterval(() => {
      const next = this.data.countdown - 1;
      if (next <= 0) {
        this.stopTimer();
        this.setData({
          countdown: 0,
          expired: true,
          errorText: '这张卡片超过 30 秒了，已进「待确认」，不会被自动记账',
        });
        return;
      }
      this.setData({ countdown: next });
    }, 1000) as unknown as number;
  },

  stopTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  },

  /** 展开/折叠「解析明细（开发用）」：默认折叠，不打扰终端用户。 */
  onToggleDevDetail(): void {
    this.setData({ devDetail: !this.data.devDetail });
  },

  /** 澄清点选（**不重调 AI**）。 */
  async onClarify(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const value = String(event.currentTarget.dataset['value'] ?? '');
    const clarify = this.data.clarify;
    if (this.data.sessionId === '' || clarify === null) {
      return;
    }
    try {
      // 点选返回的是 `ClarifyAnswerPayload`（不含卡片、不含 asr_text）：
      // 澄清完成后卡片已经建好，直接按 sessionId 重新拉一次卡片即可。
      const answered: ClarifyAnswerPayload = await answerClarify(this.data.sessionId, {
        slot: clarify.slot,
        value,
      });
      if (answered.route === 'manual') {
        wx.redirectTo({ url: `/pages/manual/index?sessionId=${this.data.sessionId}` });
        return;
      }
      if (answered.clarify !== null && answered.clarify !== undefined) {
        this.setData({ clarify: answered.clarify });
        return;
      }
      if (answered.finished) {
        await this.resolveCardFromSession();
        return;
      }
      this.setData({ errorText: `还差一项没确认（${answered.route}）` });
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** C2 二次语音：说「确认」；**按钮兜底永远在**（就绪后主按钮也能过）。 */
  async onSecondVoice(): Promise<void> {
    if (this.data.sessionId === '') {
      this.setData({ secondVoiceDone: true, errorText: '' });
      return;
    }
    try {
      const envelope = await confirm2(this.data.sessionId, { spoken_text: '确认' });
      const outcome = envelope.result;
      this.setData({
        secondVoiceDone: true,
        errorText: outcome.confirmed ? '' : `二次确认没通过（${outcome.error_code ?? '原因未知'}）`,
      });
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** 主按钮 = 确认落账（**唯一写入口**）。 */
  async onConfirm(): Promise<void> {
    if (this.data.expired) {
      this.setData({ errorText: '卡片已过期：请从「待确认」重新打开（复用同一张卡，不重复扣次）' });
      return;
    }
    const card = this.data.card;
    if (card === null) {
      return;
    }
    const isC2 = card.intent.confirm_policy === 'C2';
    this.setData({ loading: true, errorText: '' });
    try {
      // 落账返回 `{result, idempotency_key}`（见 app/api/v1/confirm.py）
      const envelope = await confirmCard(card.meta.card_id, {
        confirm_type: isC2 && this.data.secondVoiceDone ? 'voice2' : 'button',
        button_fallback: isC2 && !this.data.secondVoiceDone,
      });
      const outcome = envelope.result;
      if (!outcome.confirmed) {
        this.setData({ errorText: outcome.detail || `没记上（${outcome.error_code ?? '未知'}）` });
        return;
      }
      this.stopTimer();
      wx.showToast({ title: `已记账 ${outcome.doc_no ?? ''}`, icon: 'success' });
      setTimeout(() => wx.navigateBack({ delta: 1 }), 800);
    } catch (error) {
      if (error instanceof ApiError && error.is('E_PREVIEW_VERSION_CONFLICT')) {
        this.setData({ errorText: '这期间库存被别的操作改过（1202），请重新打开卡片预演一次' });
      } else if (error instanceof ApiError && error.is('E_CARD_EXPIRED')) {
        this.setData({ errorText: '卡片已过期，请从「待确认」重新打开' });
      } else {
        this.setData({ errorText: this.messageOf(error) });
      }
    } finally {
      this.setData({ loading: false });
    }
  },

  /** 先不记：不落账（扣次发生在建卡成功时，**不退款**）。 */
  async onCancel(): Promise<void> {
    this.stopTimer();
    const card = this.data.card;
    if (card !== null) {
      try {
        await cancelCard(card.meta.card_id);
      } catch (error) {
        console.warn('[card] 取消失败（不影响不落账）', error);
      }
    }
    wx.navigateBack({ delta: 1 });
  },

  messageOf(error: unknown): string {
    if (error instanceof ApiError) {
      return error.message || error.errorCode;
    }
    if (error instanceof Error) {
      return error.message;
    }
    return String(error);
  },
});
