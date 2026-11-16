/**
 * 展示层格式化工具（端侧）。
 *
 * 与 `packages/shared/utils/money.ts`、`date.ts`、`unit.ts` 同语义：
 * 这里只做**展示**（不参与落账计算），落账金额一律用整数分从服务端来。
 */

/** 分 → 元字符串（固定两位小数）。例：1234 -> "12.34" */
export function fenToYuanStr(fen: number): string {
  const negative = fen < 0;
  const absolute = Math.abs(Math.trunc(fen));
  const yuan = Math.floor(absolute / 100);
  const cents = absolute % 100;
  const body = `${yuan}.${cents.toString().padStart(2, '0')}`;
  return negative ? `-${body}` : body;
}

/** 分 → 带 ¥ 的文案。例：1234 -> "¥12.34" */
export function formatFen(fen: number, symbol = '¥'): string {
  const text = fenToYuanStr(fen);
  return text.startsWith('-') ? `-${symbol}${text.slice(1)}` : `${symbol}${text}`;
}

/** ISO 8601 -> 本地「MM-DD HH:mm」（列表/卡片用）。 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  const pad = (value: number): string => value.toString().padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** ISO 8601 -> 「YYYY-MM-DD」（业务日期回显，禁止显示「昨天」）。 */
export function formatBusinessDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  const pad = (value: number): string => value.toString().padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * 单位双显：「2 箱 = 48 瓶」；无换算系数时提示待教。
 *
 * @param originalQuantity 原话数量
 * @param originalUnit 原话单位
 * @param convertedQuantity 换算后数量（null 表示无系数）
 * @param baseUnit 商品基本单位
 */
export function dualUnitText(
  originalQuantity: number,
  originalUnit: string,
  convertedQuantity: number | null,
  baseUnit: string,
): string {
  if (convertedQuantity === null) {
    return `${originalQuantity} ${originalUnit}（${baseUnit} 换算待教）`;
  }
  return `${originalQuantity} ${originalUnit} = ${convertedQuantity} ${baseUnit}`;
}

/** 数量展示：整数不带小数点，小数最多两位。 */
export function formatQuantity(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

/** 屏幕形态 -> 布局类名（电脑版微信大屏用双栏，无独立网页端）。 */
export function layoutClass(windowWidth: number): string {
  return windowWidth >= 768 ? 'wide-layout' : 'container';
}
