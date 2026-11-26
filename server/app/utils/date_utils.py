"""日期工具（服务端）——与 ``packages/shared/utils/date.ts`` 严格同语义。

铁律（架构 §8.4）
----------------
1. 存储与传输用 ISO 8601 UTC；
2. 展示按租户时区（``Asia/Shanghai``，需核实）转本地；
3. 相对时间（今天/昨天/上月/上次）必须解析为**具体日期**并回显；
4. 跨月/跨年边界必须提示确认（``needs_confirm=True``），「上次」未绑定必须返回 ``None``。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "RelativeDateResult",
    "is_iso_date",
    "to_iso_date",
    "from_iso_date",
    "add_days",
    "add_months",
    "start_of_month",
    "end_of_month",
    "resolve_relative_date",
    "to_utc_iso",
    "parse_iso_datetime",
    "now_utc",
    "today_in_tz",
    "DEFAULT_TENANT_TZ",
]

#: 租户默认时区（需核实；见架构 §8.4 时间约定）
DEFAULT_TENANT_TZ = "Asia/Shanghai"

_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_OF_MONTH_PATTERN = re.compile(r"^(\d{1,2})[号日]$")


@dataclass(frozen=True)
class RelativeDateResult:
    """相对日期解析结果。"""

    date: str | None
    matched_rule: str
    needs_confirm: bool
    raw: str


def is_iso_date(value: str) -> bool:
    """校验是否为合法 ``YYYY-MM-DD``（含零填充）。"""
    if not isinstance(value, str) or not _ISO_DATE_PATTERN.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def to_iso_date(value: date) -> str:
    """``date`` -> ``YYYY-MM-DD``。"""
    return value.isoformat()


def from_iso_date(value: str) -> date:
    """``YYYY-MM-DD`` -> ``date``。

    Raises:
        ValueError: 非法日期。
    """
    if not is_iso_date(value):
        raise ValueError(f"from_iso_date: 非法日期 {value}")
    return date.fromisoformat(value)


def add_days(iso_date: str, days: int) -> str:
    """加减天数（跨月/跨年安全）。"""
    return (from_iso_date(iso_date) + timedelta(days=days)).isoformat()


def add_months(iso_date: str, months: int) -> str:
    """月份偏移；日号超界取目标月最后一天（如 3-31 减一月 -> 2-28/29）。"""
    base = from_iso_date(iso_date)
    total_months = base.year * 12 + (base.month - 1) + months
    year, month_index = divmod(total_months, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base.day, last_day)).isoformat()


def start_of_month(iso_date: str) -> str:
    """当月第一天。"""
    base = from_iso_date(iso_date)
    return date(base.year, base.month, 1).isoformat()


def end_of_month(iso_date: str) -> str:
    """当月最后一天。"""
    base = from_iso_date(iso_date)
    last_day = calendar.monthrange(base.year, base.month)[1]
    return date(base.year, base.month, last_day).isoformat()


def _result(raw: str, date_value: str | None, rule: str, needs_confirm: bool) -> RelativeDateResult:
    """构造解析结果（内部小工具，减少重复字面量）。"""
    return RelativeDateResult(
        date=date_value,
        matched_rule=rule,
        needs_confirm=needs_confirm,
        raw=raw,
    )


def resolve_relative_date(
    text: str,
    today_iso: str,
    last_date_iso: str | None = None,
) -> RelativeDateResult:
    """把相对时间口语解析为具体日期。

    Args:
        text: 用户原话片段（「昨天」「3 号」「上月」「上次」…）。
        today_iso: 今天（业务日），``YYYY-MM-DD``。
        last_date_iso: 「上次 / 上一单 / 刚才」需要绑定的最近一单日期；
            未提供则返回 ``None`` + ``needs_confirm=True``（禁止猜测）。

    Returns:
        :class:`RelativeDateResult`
    """
    if not is_iso_date(today_iso):
        raise ValueError(f"resolve_relative_date: 非法 today {today_iso}")

    normalized = re.sub(r"\s+", "", text or "")

    if normalized in {"", "今天", "今儿", "本日"}:
        return _result(text, today_iso, "today", False)
    if normalized in {"昨天", "昨儿"}:
        return _result(text, add_days(today_iso, -1), "yesterday", False)
    if normalized == "前天":
        return _result(text, add_days(today_iso, -2), "day_before_yesterday", False)
    if normalized == "大前天":
        return _result(text, add_days(today_iso, -3), "three_days_ago", False)
    if normalized in {"明天", "明儿"}:
        return _result(text, add_days(today_iso, 1), "tomorrow", False)
    if normalized == "后天":
        return _result(text, add_days(today_iso, 2), "day_after_tomorrow", False)
    if normalized in {"本月", "这个月"}:
        return _result(text, start_of_month(today_iso), "this_month_start", False)
    if normalized in {"上月", "上个月"}:
        # 跨月边界：回显必须提示确认
        return _result(text, add_months(today_iso, -1), "last_month_same_day", True)
    if normalized == "上周":
        return _result(text, add_days(today_iso, -7), "last_week", True)
    if normalized == "去年":
        return _result(text, add_months(today_iso, -12), "last_year_same_day", True)
    if normalized in {"上次", "上一单", "刚才", "那笔"}:
        if last_date_iso is not None and is_iso_date(last_date_iso):
            return _result(text, last_date_iso, "last_reference", True)
        return _result(text, None, "last_reference_unbound", True)

    day_matched = _DAY_OF_MONTH_PATTERN.match(normalized)
    if day_matched is not None:
        day = int(day_matched.group(1))
        if day < 1 or day > 31:
            return _result(text, None, "invalid_day_of_month", True)
        today = from_iso_date(today_iso)
        candidate = date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
        if day <= today.day and candidate.day == day:
            return _result(text, candidate.isoformat(), "day_of_month", False)
        previous = add_months(start_of_month(today_iso), -1)
        previous_year, previous_month = (int(part) for part in previous.split("-")[:2])
        if day <= calendar.monthrange(previous_year, previous_month)[1]:
            fallback = date(previous_year, previous_month, day).isoformat()
            return _result(text, fallback, "day_of_month_previous_month", True)
        return _result(text, None, "invalid_day_of_month", True)

    if is_iso_date(normalized):
        return _result(text, normalized, "explicit_iso_date", False)

    return _result(text, None, "unrecognized", True)


def to_utc_iso(value: datetime) -> str:
    """``datetime`` -> ISO 8601 UTC（秒精度，``Z`` 结尾）。

    无时区信息的 ``datetime`` 视为 UTC（存储口径统一为 UTC）。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_datetime(value: str) -> datetime:
    """解析 ISO 8601 字符串为带时区的 ``datetime``。

    Raises:
        ValueError: 非法时间字符串。
    """
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"parse_iso_datetime: 非法时间 {value}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(tz=timezone.utc)


def today_in_tz(tz_name: str = DEFAULT_TENANT_TZ, now: datetime | None = None) -> str:
    """按租户时区取「今天」的业务日期（``YYYY-MM-DD``）。

    Args:
        tz_name: 时区名，默认 ``Asia/Shanghai``（需核实）。
        now: 注入用当前时间（测试固定时间）。缺省取系统 UTC 时间。

    Returns:
        业务日期字符串。
    """
    current = now if now is not None else now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        tzinfo = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:  # 容器缺 tzdata 时，中国时区固定回退 UTC+8（避免早 0~8 点记成昨天）
        tzinfo = (
            timezone(timedelta(hours=8))
            if tz_name in ("Asia/Shanghai", "PRC", "Asia/Chongqing", "Asia/Harbin")
            else timezone.utc
        )
    return current.astimezone(tzinfo).date().isoformat()
