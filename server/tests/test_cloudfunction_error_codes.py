"""云函数侧的错误码不许漂移：**只能引用契约里已登记的那些**。

为什么需要这条（QA 实测踩到）
----------------------------
`xpay_deliver_notify/index.js` 曾经返回 `code: 1501` + `error_code: 'E_DELIVER_NOT_WIRED'`：

- 契约里 `1501` 已经是 ``E_DUPLICATE_REQUEST``（http 409）—— 同一个数字两个含义，
  排查时必然误导；
- ``E_DELIVER_NOT_WIRED`` 根本不在 25 条里 —— 用户拿到的错误码在文档中查不到。

「云函数没配好」是**运维配置错误**，用现成的 ``E_INTERNAL``(1503) 就够，不需要新码。

本文件用正则扫 JS（不能 import），断言：
1. 契约错误码总数仍是 25（加码必须走文档评审，不是随手加一行）；
2. JS 里出现的每个 ``E_*`` 字面量都已登记；
3. ``CODES`` 里出现的每个数字都与契约的 ``numeric`` 对得上（且不重复占用）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
ERROR_CODES_PATH = REPO_ROOT / "packages" / "shared" / "contracts" / "error-codes.json"
CLOUDFUNCTION = REPO_ROOT / "cloudfunctions" / "xpay_deliver_notify" / "index.js"

#: 契约冻结的错误码总数
EXPECTED_ERROR_CODE_COUNT = 25


def _contract() -> dict:
    """读错误码契约（单一真相源）。"""
    return json.loads(ERROR_CODES_PATH.read_text(encoding="utf-8"))


def _contract_codes() -> dict[str, dict]:
    """``{code: spec}``。"""
    return dict(_contract()["codes"])


def _cloudfunction_source() -> str:
    """云函数源码。"""
    return CLOUDFUNCTION.read_text(encoding="utf-8")


def test_contract_still_has_25_codes() -> None:
    """错误码总数不被顺手扩大（新增码要走文档评审）。"""
    doc = _contract()
    assert doc["count"] == EXPECTED_ERROR_CODE_COUNT
    assert len(doc["codes"]) == EXPECTED_ERROR_CODE_COUNT


def test_numeric_codes_are_unique() -> None:
    """同一个 numeric 不许有两个含义（1501 冲突的根因）。"""
    seen: dict[int, str] = {}
    clashes: list[str] = []
    for code, spec in _contract_codes().items():
        numeric = int(spec["numeric"])
        if numeric in seen:
            clashes.append(f"{numeric}: {seen[numeric]} vs {code}")
        seen[numeric] = code
    assert clashes == [], f"numeric 重复占用：{clashes}"


def test_cloudfunction_only_uses_registered_error_codes() -> None:
    """云函数里出现的每个 ``E_*`` 字面量都必须是已登记码。"""
    known = set(_contract_codes())
    found = set(re.findall(r"'(E_[A-Z_]+)'", _cloudfunction_source()))
    unknown = sorted(found - known)
    assert unknown == [], f"云函数用了未登记的错误码：{unknown}（禁止自造码）"
    assert found, "一个 E_* 都没扫到：正则或文件路径坏了"


def test_cloudfunction_numeric_codes_match_the_contract() -> None:
    """``CODES`` 里的数字必须与契约的 numeric 一致，且 ``1501`` 不许被冒用。"""
    contract = _contract_codes()
    numeric_of = {code: int(spec["numeric"]) for code, spec in contract.items()}
    source = _cloudfunction_source()

    block = re.search(r"const CODES = \{(?P<body>.*?)\};", source, re.S)
    assert block is not None, "云函数里找不到 CODES 常量"
    success_code = int(_contract()["success_code"])
    pairs = re.findall(r"(\w+):\s*(\d+)", block.group("body"))
    assert pairs, "CODES 里没解析出任何数字"
    for name, value in pairs:
        numeric = int(value)
        if numeric == success_code:
            assert name == "OK", f"CODES.{name} 用了成功码 {success_code}"
            continue
        assert numeric in numeric_of.values(), f"CODES.{name} = {numeric} 不在契约里"

    # 1501 是 E_DUPLICATE_REQUEST 的专用数字，云函数不许拿它表示别的意思
    assert "1501" not in block.group("body"), (
        "1501 = E_DUPLICATE_REQUEST(http 409)，不许用来表示「发货未接通」"
    )
