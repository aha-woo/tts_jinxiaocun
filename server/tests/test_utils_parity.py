"""双端工具语义一致（T01 验收 ⑤）。

用例表 ``packages/shared/utils/parity-cases.json`` 是语言中立的：本测试校验 Python 侧，
端侧 TS 单测（T05 接入 CI）读同一份表。若某侧改动导致不一致，这里会先红。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfoNotFoundError

import pytest

from app.utils import date_utils
from app.utils.date_utils import add_days, add_months, resolve_relative_date, today_in_tz
from app.utils.money import fen_to_yuan_str, format_fen, parse_cny_text, yuan_to_fen
from app.utils.unit import convert_quantity, format_dual_unit, normalize_unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_UTILS = REPO_ROOT / "packages" / "shared" / "utils"

with (SHARED_UTILS / "parity-cases.json").open("r", encoding="utf-8") as _handle:
    CASES: dict[str, Any] = json.load(_handle)

MONEY_FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "yuan_to_fen": yuan_to_fen,
    "fen_to_yuan_str": fen_to_yuan_str,
    "format_fen": format_fen,
    "parse_cny_text": parse_cny_text,
}


def test_parity_cases_file_is_shared_source() -> None:
    """用例表存在且三块（money/date/unit）都有用例，防止被误删后测试假绿。"""
    assert CASES["version"].startswith("1.1")
    assert len(CASES["money"]["cases"]) >= 20
    assert len(CASES["date"]["cases"]) >= 10
    assert len(CASES["unit"]["normalize_cases"]) >= 10
    assert len(CASES["unit"]["convert_cases"]) >= 5


@pytest.mark.parametrize("case", CASES["money"]["cases"])
def test_money_parity(case: dict[str, Any]) -> None:
    """金额：元↔分四舍五入、格式化、中文金额口语解析。"""
    func = MONEY_FUNCTIONS[case["fn"]]
    result = func(case["input"])
    assert result == case["expect"], f"{case['fn']}({case['input']!r}) => {result!r} != {case['expect']!r}"


@pytest.mark.parametrize("case", CASES["date"]["cases"])
def test_date_parity(case: dict[str, Any]) -> None:
    """日期：相对时间必须解析为具体日期，跨月/未绑定必须 needs_confirm。"""
    result = resolve_relative_date(
        case["text"],
        CASES["date"]["today"],
        case.get("last_date"),
    )
    assert result.date == case["expect"], f"{case['text']!r} => {result.date!r}"
    assert result.needs_confirm is case["needs_confirm"], f"{case['text']!r} needs_confirm"
    assert result.raw == case["text"]


@pytest.mark.parametrize("case", CASES["date"]["month_boundary_cases"])
def test_date_month_boundary(case: dict[str, Any]) -> None:
    """跨月/跨年边界：上月同日、月末钳制、跨年昨天。"""
    result = resolve_relative_date(case["text"], case["today"])
    assert result.date == case["expect"]
    assert result.needs_confirm is case["needs_confirm"]


def test_date_helpers_roundtrip() -> None:
    """日期助手：加减天数/月份安全。"""
    assert add_days("2025-11-15", -1) == "2025-11-14"
    assert add_days("2025-01-01", -1) == "2024-12-31"
    assert add_months("2025-03-31", -1) == "2025-02-28"
    assert add_months("2024-03-31", -1) == "2024-02-29"
    assert add_months("2025-11-15", 12) == "2026-11-15"


@pytest.mark.parametrize("case", CASES["unit"]["normalize_cases"])
def test_unit_normalize_parity(case: dict[str, Any]) -> None:
    """单位归一：只处理拼写变体，跨量纲必须返回 None（不臆造）。"""
    assert normalize_unit(case["input"]) == case["expect"], case["input"]


@pytest.mark.parametrize("case", CASES["unit"]["convert_cases"])
def test_unit_convert_parity(case: dict[str, Any]) -> None:
    """单位换算：有系数才换算，无系数返回 None + needs_teaching=True。"""
    result = convert_quantity(
        case["quantity"],
        case["from"],
        case["to"],
        case["conversions"],
    )
    if case["expect"] is None:
        assert result.ok is False
        assert result.quantity is None
        assert result.ratio is None
    else:
        assert result.ok is True
        assert result.quantity == pytest.approx(case["expect"])
        assert result.ratio is not None
    assert result.needs_teaching is case["needs_teaching"]


def test_unit_dual_display() -> None:
    """双显文案：「2 箱 = 48 瓶」；无系数时提示待教。"""
    converted = convert_quantity(2, "箱", "瓶", {"箱": {"瓶": 24}})
    assert format_dual_unit(2, "箱", converted) == "2 箱 = 48 瓶"
    failed = convert_quantity(2, "箱", "斤", {})
    assert "待教" in format_dual_unit(2, "箱", failed)


def test_ts_utils_carry_same_api() -> None:
    """端侧三份工具必须存在，且导出与服务端同名的核心函数（防止单端漂移）。"""
    money_ts = (SHARED_UTILS / "money.ts").read_text(encoding="utf-8")
    date_ts = (SHARED_UTILS / "date.ts").read_text(encoding="utf-8")
    unit_ts = (SHARED_UTILS / "unit.ts").read_text(encoding="utf-8")

    for symbol in ["yuanToFen", "fenToYuanStr", "formatFen", "parseCnyText"]:
        assert f"export function {symbol}" in money_ts, symbol
    for symbol in ["resolveRelativeDate", "addDays", "addMonths", "toUtcIso"]:
        assert f"export function {symbol}" in date_ts, symbol
    for symbol in ["normalizeUnit", "convertQuantity", "formatDualUnit"]:
        assert f"export function {symbol}" in unit_ts, symbol

    # 金额侧必须声明四舍五入模式（两端都要 ROUND_HALF_UP），不得用浮点乘 100
    assert "ROUND_HALF_UP" in money_ts
    assert "Math.round" in money_ts
    # 单位侧必须强调换算系数只来自 unit_conversion，禁止硬编码系数
    assert "unit_conversion" in unit_ts
    assert "needsTeaching" in unit_ts
    # 日期侧必须强调 ISO 8601 与跨月提示
    assert "ISO 8601" in date_ts
    assert "needsConfirm" in date_ts


def test_money_never_uses_float_multiply() -> None:
    """服务端金额实现必须走 Decimal，禁止 float * 100 的隐式精度损失。"""
    money_py = (Path(__file__).resolve().parents[1] / "app" / "utils" / "money.py").read_text(encoding="utf-8")
    assert "ROUND_HALF_UP" in money_py
    assert "Decimal" in money_py


def test_today_in_tz_falls_back_to_utc_plus_8_when_tzdata_missing(monkeypatch) -> None:
    """容器缺 tzdata 时，中国时区必须回退 UTC+8（防止早 0~8 点业务日倒退成昨天）。"""
    def _missing_zone(_name: str) -> Any:
        raise ZoneInfoNotFoundError("no tzdata")

    monkeypatch.setattr(date_utils, "ZoneInfo", _missing_zone)
    # UTC 2026-11-26 23:30 = 北京时间 2026-11-27 07:30
    utc_now = datetime(2026, 11, 26, 23, 30, tzinfo=timezone.utc)
    assert today_in_tz("Asia/Shanghai", now=utc_now) == "2026-11-27"
    assert today_in_tz("PRC", now=utc_now) == "2026-11-27"
    assert today_in_tz("UTC", now=utc_now) == "2026-11-26"


def test_container_bundles_tzdata() -> None:
    """容器镜像与依赖声明都必须包含 tzdata，消除 psycopg PRC 时区告警。"""
    server_root = Path(__file__).resolve().parents[1]
    assert "tzdata" in (server_root / "Dockerfile").read_text(encoding="utf-8")
    assert "tzdata" in (server_root / "pyproject.toml").read_text(encoding="utf-8")
