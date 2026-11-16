/**
 * 日期工具（端侧）——与 server/app/utils/date_utils.py 严格同语义。
 *
 * 铁律（docs/02-架构设计.md §8.4）：
 * 1) 存储与传输用 ISO 8601 UTC（`2025-11-15T02:30:00Z`）；
 * 2) 展示按租户时区（Asia/Shanghai，需核实）转本地；
 * 3) 相对时间（今天/昨天/上月/上次）必须解析为**具体日期**并回显；
 * 4) 跨月/跨年边界必须提示确认（needs_confirm = true）。
 */

/** 相对日期解析结果。 */
export interface RelativeDateResult {
  /** 解析出的具体日期 YYYY-MM-DD；无法确定时为 null。 */
  date: string | null;
  /** 命中的规则名（用于回显与埋点）。 */
  matchedRule: string;
  /** 是否需要用户确认（跨月/跨年/需绑定单据）。 */
  needsConfirm: boolean;
  /** 原始文本。 */
  raw: string;
}

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function pad2(value: number): string {
  return value.toString().padStart(2, '0');
}

/** Date -> YYYY-MM-DD（使用 UTC 字段，避免本地时区漂移）。 */
export function toIsoDate(date: Date): string {
  return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;
}

/** YYYY-MM-DD -> Date（UTC 零点）。 */
export function fromIsoDate(iso: string): Date {
  if (!ISO_DATE_PATTERN.test(iso)) {
    throw new RangeError(`fromIsoDate: 非法日期 ${iso}`);
  }
  const [year, month, day] = iso.split('-').map((part) => Number.parseInt(part, 10));
  return new Date(Date.UTC(year, month - 1, day));
}

/** 校验是否为合法 YYYY-MM-DD。 */
export function isIsoDate(value: string): boolean {
  if (!ISO_DATE_PATTERN.test(value)) {
    return false;
  }
  const date = fromIsoDate(value);
  return toIsoDate(date) === value;
}

/** 加减天数（跨月/跨年安全）。 */
export function addDays(iso: string, days: number): string {
  const date = fromIsoDate(iso);
  date.setUTCDate(date.getUTCDate() + days);
  return toIsoDate(date);
}

/** 月份偏移，日号超界时取当月最后一天（如 1-31 减一个月 -> 12-31 / 2-28）。 */
export function addMonths(iso: string, months: number): string {
  const date = fromIsoDate(iso);
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth();
  const day = date.getUTCDate();
  const target = new Date(Date.UTC(year, month + months, 1));
  const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  target.setUTCDate(Math.min(day, lastDay));
  return toIsoDate(target);
}

/** 当月第一天。 */
export function startOfMonth(iso: string): string {
  return `${iso.slice(0, 7)}-01`;
}

/** 当月最后一天。 */
export function endOfMonth(iso: string): string {
  const date = fromIsoDate(iso);
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  return `${iso.slice(0, 7)}-${pad2(lastDay)}`;
}

/**
 * 解析相对日期。
 *
 * @param text 用户原话片段（如「昨天」「3 号」「上月」「上次」）
 * @param todayIso 今天（业务日），YYYY-MM-DD
 * @param lastDateIso 可选：「上次 / 上一单 / 刚才」需要绑定的最近一单日期；
 *                    未提供则返回 null + needsConfirm=true（禁止猜测）
 */
export function resolveRelativeDate(
  text: string,
  todayIso: string,
  lastDateIso: string | null = null,
): RelativeDateResult {
  const raw = text;
  const normalized = text.replace(/\s+/g, '');
  const base = (date: string | null, rule: string, needsConfirm: boolean): RelativeDateResult => ({
    date,
    matchedRule: rule,
    needsConfirm,
    raw,
  });

  if (normalized === '' || normalized === '今天' || normalized === '今儿' || normalized === '本日') {
    return base(todayIso, 'today', false);
  }
  if (normalized === '昨天' || normalized === '昨儿') {
    return base(addDays(todayIso, -1), 'yesterday', false);
  }
  if (normalized === '前天') {
    return base(addDays(todayIso, -2), 'day_before_yesterday', false);
  }
  if (normalized === '大前天') {
    return base(addDays(todayIso, -3), 'three_days_ago', false);
  }
  if (normalized === '明天' || normalized === '明儿') {
    return base(addDays(todayIso, 1), 'tomorrow', false);
  }
  if (normalized === '后天') {
    return base(addDays(todayIso, 2), 'day_after_tomorrow', false);
  }
  if (normalized === '本月' || normalized === '这个月') {
    return base(startOfMonth(todayIso), 'this_month_start', false);
  }
  if (normalized === '上月' || normalized === '上个月') {
    // 跨月边界：回显必须提示确认
    return base(addMonths(todayIso, -1), 'last_month_same_day', true);
  }
  if (normalized === '上周') {
    return base(addDays(todayIso, -7), 'last_week', true);
  }
  if (normalized === '去年') {
    return base(addMonths(todayIso, -12), 'last_year_same_day', true);
  }
  if (normalized === '上次' || normalized === '上一单' || normalized === '刚才' || normalized === '那笔') {
    if (lastDateIso !== null && isIsoDate(lastDateIso)) {
      return base(lastDateIso, 'last_reference', true);
    }
    return base(null, 'last_reference_unbound', true);
  }

  const dayOfMonth = /^(\d{1,2})[号日]$/.exec(normalized);
  if (dayOfMonth !== null) {
    const day = Number.parseInt(dayOfMonth[1], 10);
    if (day < 1 || day > 31) {
      return base(null, 'invalid_day_of_month', true);
    }
    const currentMonth = todayIso.slice(0, 7);
    const candidate = `${currentMonth}-${pad2(day)}`;
    if (isIsoDate(candidate) && day <= Number.parseInt(todayIso.slice(8, 10), 10)) {
      return base(candidate, 'day_of_month', false);
    }
    const previousMonth = addMonths(startOfMonth(todayIso), -1).slice(0, 7);
    const fallback = `${previousMonth}-${pad2(day)}`;
    if (isIsoDate(fallback)) {
      // 本月还没到这一天，只能理解为上个月 -> 跨月必须提示确认
      return base(fallback, 'day_of_month_previous_month', true);
    }
    return base(null, 'invalid_day_of_month', true);
  }

  if (isIsoDate(normalized)) {
    return base(normalized, 'explicit_iso_date', false);
  }

  return base(null, 'unrecognized', true);
}

/** ISO 8601 UTC 字符串（秒精度）：2025-11-15T02:30:00Z */
export function toUtcIso(date: Date): string {
  return `${date.toISOString().slice(0, 19)}Z`;
}

/** 解析 ISO 8601 字符串为 Date；非法抛错。 */
export function parseIsoDateTime(value: string): Date {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    throw new RangeError(`parseIsoDateTime: 非法时间 ${value}`);
  }
  return date;
}
