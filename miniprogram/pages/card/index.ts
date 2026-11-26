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
  editCard,
  fieldVoiceCard,
  getCard,
  listPending,
  reviveCard,
  type CardPayload,
  type ClarifyAnswerPayload,
  type ClarifyPayload,
  type NoviceStatus,
  type PendingGap,
} from '../../services/api';
import { SpeechGate, type SpeechResult } from '../../services/speech';
import { appendNumericKey } from '../../utils/format';
import { isWideScreen } from '../../utils/screen';
import type { SheetActionItem } from '../../components/action-sheet/action-sheet';

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
export function buildInfoFields(card: CardPayload): { code: string; label: string; value: string }[] {
  const rows = card.slots ?? [];
  const result: { code: string; label: string; value: string }[] = [];
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
    result.push({ code: field.code, label: field.label, value });
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
  infoFields: { code: string; label: string; value: string }[];
  /** 商品还没有档案：不能直接确认，要先建商品 */
  hasPendingProduct: boolean;
  /** 是否展开「解析明细（开发用）」——默认折叠 */
  devDetail: boolean;
  /** 服务端算出的「要补」缺口 */
  gaps: PendingGap[];
  /** 有红色缺口时禁止确认 */
  hasMustGap: boolean;
  /** 正在补的那一格 */
  editingGap: PendingGap | null;
  gapKeypad: string;
  /** 新手期进度徽标（例如「新手期 3/20」） */
  noviceText: string;
  /** 近似已有商品候选（「店里有可乐，是它吗？」） */
  similarProducts: { product_id: string; name: string }[];
  similarDismissed: boolean;
  /** 字段语音重试中 */
  voiceFieldSlot: string;
  voiceListening: boolean;
  sheet: {
    visible: boolean;
    title: string;
    subtitle: string;
    actions: SheetActionItem[];
  };
}

/** 只把这四个槽位展示给用户；其余是内部字段。 */
const INFO_FIELDS: { code: string; label: string }[] = [
  { code: 'D', label: '日期' },
  { code: 'PS', label: '付款' },
  { code: 'S', label: '供应商' },
  { code: 'C', label: '客户' },
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
    gaps: [],
    hasMustGap: false,
    editingGap: null,
    gapKeypad: '',
    devDetail: false,
    noviceText: '',
    similarProducts: [],
    similarDismissed: false,
    voiceFieldSlot: '',
    voiceListening: false,
    sheet: { visible: false, title: '', subtitle: '', actions: [] },
  },

  fieldSpeechGate: null as SpeechGate | null,
  sheetCtx: null as { mode: 'line' } | { mode: 'ps' } | null,

  onLoad(query: Record<string, string | undefined>) {
    const cardId = String(query['cardId'] ?? '');
    const sessionId = String(query['sessionId'] ?? '');
    const novice = wx.getStorageSync('dsh:novice') as NoviceStatus | '' | undefined;
    const noviceText =
      novice && typeof novice === 'object' && novice.in_novice
        ? `新手期 ${novice.count}/${novice.target}`
        : '';
    this.setData({ cardId, sessionId, noviceText });
    const app = getApp<{ globalData: { screen: 'phone' | 'wide' } }>();
    this.setData({ wide: app.globalData.screen === 'wide' || isWideScreen() });
    if (cardId !== '') {
      void this.loadCard(cardId);
    } else {
      // 从澄清流程进来：澄清内容由首页暂存，这里按 sessionId 取回
      const saved = wx.getStorageSync('dsh:pendingClarify') as
        | { sessionId?: string; clarify?: ClarifyPayload }
        | ''
        | undefined;
      if (
        sessionId !== '' &&
        saved !== '' &&
        saved !== undefined &&
        saved.sessionId === sessionId &&
        saved.clarify !== undefined
      ) {
        this.setData({ loading: false, clarify: saved.clarify });
      } else {
        this.setData({ loading: false, errorText: '没有找到这张卡，回首页重新说一句' });
      }
    }
  },

  onResize() {
    this.setData({ wide: isWideScreen() });
  },

  onUnload() {
    this.fieldSpeechGate?.dispose();
    this.fieldSpeechGate = null;
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
      const similar = (card.calc.lines[0]?.similar_products ?? []) as { product_id: string; name: string }[];
      this.setData({
        card,
        gaps: card.gaps ?? [],
        hasMustGap: (card.gaps ?? []).some((gap) => gap.must),
        directionText: direction.text === '' ? card.intent.intent_label_zh : direction.text,
        cardTitle:
          direction.text === ''
            ? card.intent.intent_label_zh
            : `${direction.text}单`,
        totalAmountText: fenToYuanText(totalFen),
        infoFields: buildInfoFields(card),
        hasPendingProduct: detectPendingProduct(card),
        similarProducts: similar,
        similarDismissed: false,
        needSecondVoice: card.intent.confirm_policy === 'C2',
        riskTone: card.risk.level === 'ok' ? 'ok' : 'warn',
        loading: false,
      });
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
      const hit = (pending.items ?? []).find((item) => item.session_id === this.data.sessionId);
      if (hit === undefined) {
        this.setData({ errorText: '澄清好了但没找到卡片，请回首页再来一次' });
        return;
      }
      wx.redirectTo({ url: `/pages/card/index?cardId=${hit.card_id}` });
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
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
        wx.removeStorageSync('dsh:pendingClarify');
        wx.redirectTo({ url: `/pages/manual/index?sessionId=${this.data.sessionId}` });
        return;
      }
      if (answered.clarify !== null && answered.clarify !== undefined) {
        this.setData({ clarify: answered.clarify });
        return;
      }
      if (answered.finished) {
        wx.removeStorageSync('dsh:pendingClarify');
        await this.resolveCardFromSession();
        return;
      }
      this.setData({ errorText: `还差一项没确认（${answered.route}）` });
    } catch (error) {
      this.setData({ errorText: this.messageOf(error) });
    }
  },

  /** 点一格缺口：数字项弹键盘，文字项弹输入框。 */
  onGapTap(event: WechatMiniprogram.TouchEvent): void {
    const code = String(event.currentTarget.dataset['code'] ?? '');
    const gap = this.data.gaps.find((item) => item.code === code);
    if (gap === undefined) {
      return;
    }
    this.openGapEditor(gap);
  },

  openGapEditor(gap: PendingGap): void {
    if (gap.code === 'S' || gap.code === 'C' || gap.code === 'P' || gap.code === 'U') {
      wx.showModal({
        title: `修改${gap.label}`,
        editable: true,
        placeholderText: `输入${gap.label}`,
        content: gap.current,
        success: (res) => {
          if (res.confirm) {
            void this.submitGap(gap.code, String(res.content ?? '').trim());
          }
        },
      });
      return;
    }
    this.setData({ editingGap: gap, gapKeypad: gap.current });
  },

  /** 点击明细行：支持直接改数量 / 单价 / 总价 / 商品名（不重调 AI）。 */
  onLineTap(): void {
    const card = this.data.card;
    if (card === null || card.calc.lines.length === 0) {
      return;
    }
    const line = card.calc.lines[0];
    this.sheetCtx = { mode: 'line' };
    this.setData({
      sheet: {
        visible: true,
        title: '修改这笔明细',
        subtitle: `${line.product_label} · ${line.quantity}${line.unit}（${line.amount_display}）`,
        actions: [
          { key: 'Q', label: '改数量', tone: 'primary' },
          { key: 'UP', label: '改单价（元）' },
          { key: 'TA', label: '改总价（元）' },
          { key: 'P', label: '改商品名' },
        ],
      },
    });
  },

  /** 点击基本信息行：支持修改付款方式、往来单位、日期。 */
  onInfoTap(event: WechatMiniprogram.TouchEvent): void {
    const code = String(event.currentTarget.dataset['code'] ?? '');
    const label = String(event.currentTarget.dataset['label'] ?? '');
    const value = String(event.currentTarget.dataset['value'] ?? '');
    if (code === '') {
      return;
    }
    if (code === 'PS') {
      this.sheetCtx = { mode: 'ps' };
      this.setData({
        sheet: {
          visible: true,
          title: '修改结款方式',
          subtitle: '选择这笔单的付款状态',
          actions: [
            { key: '已结', label: '已付清（现结）', tone: 'primary' },
            { key: '未结', label: '还欠着（赊账）' },
            { key: '部分', label: '付了一部分' },
          ],
        },
      });
      return;
    }
    this.openGapEditor({ code, label, unit: '', must: false, current: value });
  },

  onSheetClose(): void {
    this.sheetCtx = null;
    this.setData({ 'sheet.visible': false });
  },

  onSheetSelect(event: WechatMiniprogram.CustomEvent<{ index: number; key: string }>): void {
    const ctx = this.sheetCtx;
    const { key } = event.detail;
    this.onSheetClose();
    if (!ctx) {
      return;
    }
    if (ctx.mode === 'ps') {
      if (key) {
        void this.submitGap('PS', key);
      }
      return;
    }
    const card = this.data.card;
    if (card === null || card.calc.lines.length === 0) {
      return;
    }
    const line = card.calc.lines[0];
    if (key === 'Q') {
      this.openGapEditor({
        code: 'Q',
        label: '数量',
        unit: line.unit || '',
        must: false,
        current: String(line.quantity ?? ''),
      });
    } else if (key === 'UP') {
      const upYuan =
        typeof line.unit_price_fen === 'number'
          ? (line.unit_price_fen / 100).toFixed(2).replace(/\.00$/, '')
          : '';
      this.openGapEditor({ code: 'UP', label: '单价', unit: '元', must: false, current: upYuan });
    } else if (key === 'TA') {
      const taYuan =
        typeof line.amount_fen === 'number'
          ? (line.amount_fen / 100).toFixed(2).replace(/\.00$/, '')
          : '';
      this.openGapEditor({ code: 'TA', label: '总价', unit: '元', must: false, current: taYuan });
    } else if (key === 'P') {
      this.openGapEditor({
        code: 'P',
        label: '商品名称',
        unit: '',
        must: false,
        current: String(line.product_label ?? ''),
      });
    }
  },

  onGapKey(event: WechatMiniprogram.TouchEvent): void {
    const key = String(event.currentTarget.dataset['key'] ?? '');
    this.setData({ gapKeypad: appendNumericKey(this.data.gapKeypad, key, 8) });
  },

  onGapKeyOk(): void {
    const gap = this.data.editingGap;
    if (gap === null || this.data.gapKeypad === '') {
      return;
    }
    void this.submitGap(gap.code, this.data.gapKeypad);
  },

  onGapClose(): void {
    this.setData({ editingGap: null, gapKeypad: '' });
  },

  async submitGap(code: string, value: string): Promise<void> {
    if (value === '' || this.data.cardId === '') {
      return;
    }
    const normalized: string | number =
      code === 'UP' || code === 'TA' || code === 'AMT'
        ? Math.round(Number(value) * 100)
        : code === 'Q' && Number.isFinite(Number(value))
          ? Number(value)
          : value;
    try {
      const edited = await editCard(this.data.cardId, [{ code, value: normalized }]);
      this.setData({ editingGap: null, gapKeypad: '' });
      wx.showToast({ title: edited.toast_zh || '补好了', icon: 'none' });
      await this.loadCard(this.data.cardId);
    } catch (error) {
      this.setData({ editingGap: null, errorText: this.messageOf(error) });
    }
  },

  /** 点「就是它」：把疑似同名/方言新词映射到店里已有商品。 */
  async onAcceptSimilar(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const name = String(event.currentTarget.dataset['name'] ?? '');
    if (name === '' || this.data.cardId === '') {
      return;
    }
    await this.submitGap('P', name);
  },

  onDismissSimilar(): void {
    this.setData({ similarDismissed: true });
  },

  /** 字段级按住重说：只重说某一格，不重调大模型。 */
  async onFieldVoiceStart(event: WechatMiniprogram.TouchEvent): Promise<void> {
    const slotCode = String(event.currentTarget.dataset['code'] ?? 'Q');
    if (this.fieldSpeechGate === null) {
      this.fieldSpeechGate = new SpeechGate();
    }
    if (!this.fieldSpeechGate.available) {
      wx.showToast({ title: '语音插件未就绪，请点格子直接输入', icon: 'none' });
      return;
    }
    this.setData({ voiceFieldSlot: slotCode, voiceListening: true });
    const outcome = await this.fieldSpeechGate.start();
    if (!outcome.granted) {
      this.setData({ voiceFieldSlot: '', voiceListening: false });
    }
  },

  async onFieldVoiceEnd(): Promise<void> {
    const slotCode = this.data.voiceFieldSlot;
    this.setData({ voiceListening: false });
    if (this.fieldSpeechGate === null || slotCode === '' || this.data.cardId === '') {
      this.setData({ voiceFieldSlot: '' });
      return;
    }
    const said: SpeechResult | null = await this.fieldSpeechGate.stop();
    this.setData({ voiceFieldSlot: '' });
    if (said === null || said.text === '') {
      return;
    }
    try {
      const updated = await fieldVoiceCard(this.data.cardId, {
        slot_code: slotCode,
        spoken_text: said.text,
      });
      wx.showToast({ title: updated.toast_zh || '已按你说的话改好', icon: 'none' });
      await this.loadCard(this.data.cardId);
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
    if (this.data.hasMustGap) {
      wx.showToast({ title: '先把红色的项填上才能确认', icon: 'none' });
      return;
    }

    if (this.data.hasPendingProduct && this.data.directionText === '卖货') {
      wx.showToast({ title: '档案里没有这件货，卖不了', icon: 'none' });
      return;
    }

    // 黄色的项（如进价）允许先记账后补，或者现在打开空格键盘补上
    const yellow = this.data.gaps.filter((gap) => !gap.must);
    if (yellow.length > 0) {
      const labels = yellow.map((gap) => gap.label).join('、');
      const postAnyway = await new Promise<boolean>((resolve) => {
        wx.showModal({
          title: '还有项没填',
          content: `${labels}还没填。可以先记账、回头在「待处理 · 要补」里补；也可以现在补上让毛利更准。`,
          confirmText: '先记账后补',
          cancelText: '现在补',
          success: (res) => resolve(Boolean(res.confirm)),
          fail: () => resolve(false),
        });
      });
      if (!postAnyway) {
        this.openGapEditor(yellow[0]);
        return;
      }
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
      if (outcome.novice !== undefined) {
        wx.setStorageSync('dsh:novice', outcome.novice);
        if (outcome.novice.just_graduated) {
          wx.setStorageSync('dsh:justGraduated', outcome.novice);
        }
      }

      wx.showToast({ title: `已记账 ${outcome.doc_no ?? ''}`, icon: 'success' });
      setTimeout(() => wx.navigateBack({ delta: 1 }), 800);
    } catch (error) {
      if (error instanceof ApiError && error.is('E_PREVIEW_VERSION_CONFLICT')) {
        this.setData({ errorText: '这期间库存被别的操作改过（1202），请重新打开卡片预演一次' });
      } else if (error instanceof ApiError && error.is('E_CARD_EXPIRED')) {
        // 卡片过了 TTL：自动复活并换用全新幂等键重试（避免命中中间件缓存的 409）
        try {
          await reviveCard(card.meta.card_id);
          const retry = await confirmCard(
            card.meta.card_id,
            {
              confirm_type: isC2 && this.data.secondVoiceDone ? 'voice2' : 'button',
              button_fallback: isC2 && !this.data.secondVoiceDone,
            },
            `confirm-revive:${card.meta.card_id}:${Date.now()}`
          );
          if (retry.result.confirmed) {
            wx.showToast({ title: `已记账 ${retry.result.doc_no ?? ''}`, icon: 'success' });
            setTimeout(() => wx.navigateBack({ delta: 1 }), 800);
            return;
          }
          this.setData({ errorText: retry.result.detail || '没记上' });
        } catch (retryError) {
          this.setData({ errorText: this.messageOf(retryError) });
        }
      } else {
        this.setData({ errorText: this.messageOf(error) });
      }
    } finally {
      this.setData({ loading: false });
    }
  },

  /** 先不记：不落账（扣次发生在建卡成功时，**不退款**）。 */
  async onCancel(): Promise<void> {

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
