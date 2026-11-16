/**
 * 单位归一工具（端侧）——与 server/app/utils/unit.py 严格同语义。
 *
 * 铁律（docs/02-架构设计.md §8.4）：
 * 1) 一律折算到商品 `base_unit`，并**双显**「原话 2 箱 = 48 瓶」；
 * 2) 换算系数只能来自 `unit_conversion`（需审核），**代码禁止臆造系数**；
 * 3) 无系数 -> ok=false + needsTeaching=true（卡片标「待教学」，进入待教学队列）。
 */

/** 已登记的标准单位（不含任何换算含义，纯单位名归一）。 */
export const CANONICAL_UNITS: readonly string[] = [
  '箱',
  '件',
  '瓶',
  '斤',
  '个',
  '条',
  '罐',
  '包',
  '桶',
  '提',
  '袋',
  '盒',
];

/** 仅处理「拼写变体」的同义写法，**绝不**跨量纲映射（公斤 ≠ 斤）。 */
const SPELLING_VARIANTS: Readonly<Record<string, string>> = {
  整箱: '箱',
  箱装: '箱',
  件装: '件',
  整件: '件',
  瓶装: '瓶',
  条装: '条',
  罐装: '罐',
  袋装: '袋',
  盒装: '盒',
  包装: '包',
};

/**
 * 单位名归一：去掉空白/标点与口语后缀（「装」「儿」），返回标准单位。
 *
 * @returns 标准单位；无法识别返回 null（调用方必须澄清，不得猜）
 */
export function normalizeUnit(text: string): string | null {
  if (typeof text !== 'string') {
    return null;
  }
  let normalized = text.trim().replace(/\s+/g, '').replace(/[，,。.!！?？]/g, '');
  if (normalized === '') {
    return null;
  }
  if (SPELLING_VARIANTS[normalized] !== undefined) {
    return SPELLING_VARIANTS[normalized];
  }
  while (normalized.endsWith('儿') || normalized.endsWith('装')) {
    normalized = normalized.slice(0, -1);
    if (SPELLING_VARIANTS[normalized] !== undefined) {
      return SPELLING_VARIANTS[normalized];
    }
  }
  if (CANONICAL_UNITS.includes(normalized)) {
    return normalized;
  }
  return null;
}

/** 换算表：`{ 箱: { 瓶: 24 } }` 表示 1 箱 = 24 瓶（只来自 unit_conversion）。 */
export type ConversionTable = Record<string, Record<string, number>>;

/** 换算结果。 */
export interface ConversionResult {
  /** 是否成功换算。 */
  ok: boolean;
  /** 换算后数量；失败为 null。 */
  quantity: number | null;
  /** 使用的系数（base 单位倍数）；失败为 null。 */
  ratio: number | null;
  /** 是否需要「教系统」（无系数）。 */
  needsTeaching: boolean;
  fromUnit: string;
  toUnit: string;
}

function roundTo6(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

/**
 * 数量单位换算。
 *
 * @param quantity 原话数量（必须 >= 0）
 * @param fromUnit 原话单位（应为 normalizeUnit 的结果）
 * @param toUnit 目标单位（商品 base_unit）
 * @param conversions 换算表（不得为空造系数）
 */
export function convertQuantity(
  quantity: number,
  fromUnit: string,
  toUnit: string,
  conversions: ConversionTable = {},
): ConversionResult {
  const failure: ConversionResult = {
    ok: false,
    quantity: null,
    ratio: null,
    needsTeaching: true,
    fromUnit,
    toUnit,
  };

  if (!Number.isFinite(quantity) || quantity < 0) {
    return failure;
  }
  if (fromUnit === toUnit) {
    return {
      ok: true,
      quantity,
      ratio: 1,
      needsTeaching: false,
      fromUnit,
      toUnit,
    };
  }

  const direct = conversions[fromUnit]?.[toUnit];
  if (typeof direct === 'number' && direct > 0) {
    return {
      ok: true,
      quantity: roundTo6(quantity * direct),
      ratio: direct,
      needsTeaching: false,
      fromUnit,
      toUnit,
    };
  }

  const inverse = conversions[toUnit]?.[fromUnit];
  if (typeof inverse === 'number' && inverse > 0) {
    return {
      ok: true,
      quantity: roundTo6(quantity / inverse),
      ratio: roundTo6(1 / inverse),
      needsTeaching: false,
      fromUnit,
      toUnit,
    };
  }

  // 无系数：绝不臆造，交回澄清/教学
  return failure;
}

/** 双显文案：「2 箱 = 48 瓶」；无系数时返回待教学文案。 */
export function formatDualUnit(
  originalQuantity: number,
  originalUnit: string,
  result: ConversionResult,
): string {
  if (result.ok && result.quantity !== null) {
    return `${originalQuantity} ${originalUnit} = ${result.quantity} ${result.toUnit}`;
  }
  return `${originalQuantity} ${originalUnit}（${result.toUnit} 换算待教）`;
}
