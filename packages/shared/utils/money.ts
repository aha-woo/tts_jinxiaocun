/**
 * 金额工具（端侧）——与 server/app/utils/money.py 严格同语义。
 *
 * 铁律（docs/02-架构设计.md §8.4）：
 * 1) 金额一律以「分」为整数存储与传输（字段名 `*_fen`）；
 * 2) 元 → 分四舍五入规则 = ROUND_HALF_UP（远离零），禁用浮点乘 100；
 * 3) 展示层才格式化，禁止浮点直接比较。
 *
 * 双端一致性由 packages/shared/utils/parity-cases.json + 两侧测试共同保证。
 */

/** 金额解析结果：拆分符号与数字，避免任何浮点运算。 */
interface DecimalParts {
  negative: boolean;
  intPart: string;
  fracPart: string;
}

const DECIMAL_PATTERN = /^([+-]?)(\d+)(?:\.(\d+))?$/;

/** 将字符串金额拆为整数部分与小数部分；非法输入返回 null。 */
function parseDecimalParts(value: string): DecimalParts | null {
  const trimmed = value.trim();
  const matched = DECIMAL_PATTERN.exec(trimmed);
  if (matched === null) {
    return null;
  }
  const sign = matched[1] === '-' ? -1 : 1;
  return {
    negative: sign < 0,
    intPart: matched[2],
    fracPart: matched[3] ?? '',
  };
}

/**
 * 元 → 分（ROUND_HALF_UP，远离零）。
 *
 * @param value 元，支持 number 或 string；推荐 string，避免调用方已丢精度
 * @throws RangeError 当入参不是合法十进制数字
 */
export function yuanToFen(value: string | number): number {
  const parts = parseDecimalParts(typeof value === 'number' ? value.toString() : value);
  if (parts === null) {
    throw new RangeError(`yuanToFen: 非法的金额输入 ${String(value)}`);
  }
  const intFen = Number.parseInt(parts.intPart, 10) * 100;
  const frac = (parts.fracPart + '000000').slice(0, 6);
  const kept = Number.parseInt(frac.slice(0, 2), 10);
  // 保留 2 位后，第一位被丢弃的数字 >= 5 则进位（half-up）
  const firstDropped = frac.charAt(2);
  const roundUp = firstDropped >= '5';
  const fen = intFen + kept + (roundUp ? 1 : 0);
  return parts.negative ? -fen : fen;
}

/** 分 → 元（浮点，仅用于展示/图表；禁止用于计算或比较）。 */
export function fenToYuan(fen: number): number {
  return fen / 100;
}

/**
 * 分 → 元字符串（固定两位小数，不带货币符号）。
 *
 * 例：1234 -> "12.34"，5 -> "0.05"，-100 -> "-1.00"
 */
export function fenToYuanStr(fen: number): string {
  if (!Number.isInteger(fen)) {
    throw new RangeError(`fenToYuanStr: 分必须为整数，收到 ${fen}`);
  }
  const negative = fen < 0;
  const abs = Math.abs(fen);
  const yuan = Math.floor(abs / 100);
  const cents = abs % 100;
  const body = `${yuan}.${cents.toString().padStart(2, '0')}`;
  return negative ? `-${body}` : body;
}

/** 分 → 带货币符号的展示文案。例：1234 -> "¥12.34"，-100 -> "-¥1.00" */
export function formatFen(fen: number, symbol = '¥'): string {
  const text = fenToYuanStr(fen);
  return text.startsWith('-') ? `-${symbol}${text.slice(1)}` : `${symbol}${text}`;
}

const CNY_UNIT_PATTERN = /^([+-]?)(\d+(?:\.\d+)?)\s*(?:块|元)(\d)?\s*(?:毛|角)?$/;
const BARE_NUMBER_PATTERN = /^([+-]?)(\d+(?:\.\d+)?)$/;

/**
 * 解析中文金额口语（只处理「数字 + 块/元」，不做中文数字归一，后者属于 T03 number_normalizer）。
 *
 * 支持：`3块5`、`3块5毛`、`12.5元`、`35`（裸数字按元处理）。
 * 不支持（返回 null）：纯中文数字（如「两块」）——必须交给 T03 归一化为阿拉伯数字。
 */
export function parseCnyText(text: string): number | null {
  const trimmed = text.trim().replace(/\s+/g, '');
  const matched = CNY_UNIT_PATTERN.exec(trimmed);
  if (matched !== null) {
    const negative = matched[1] === '-';
    const yuanFen = yuanToFen(matched[2]);
    const jiaoDigit = matched[3];
    const jiaoFen = jiaoDigit === undefined ? 0 : Number.parseInt(jiaoDigit, 10) * 10;
    const total = yuanFen + jiaoFen;
    return negative ? -total : total;
  }
  const bare = BARE_NUMBER_PATTERN.exec(trimmed);
  if (bare !== null) {
    return yuanToFen(`${bare[1]}${bare[2]}`);
  }
  return null;
}

/** 计算式字符串（卡片必须随金额返回计算式）。 */
export function calcTrace(quantity: number, unitPriceFen: number, unitLabel = ''): string {
  const qtyText = unitLabel === '' ? String(quantity) : `${quantity} ${unitLabel}`;
  const total = Math.round(quantity * unitPriceFen);
  return `${qtyText} × ${formatFen(unitPriceFen)} = ${formatFen(total)}`;
}
