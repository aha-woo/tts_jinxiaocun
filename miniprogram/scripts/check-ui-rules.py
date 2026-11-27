#!/usr/bin/env python
"""小程序 UI 与文案规范静态巡检脚本。

可在仓库根目录直接运行（纯 Python 标准库，无第三方依赖）：

    python miniprogram/scripts/check-ui-rules.py

检查项：
1. ``STYLE_D_NO_EMOJI``：页面与组件代码中禁止使用系统 Emoji 作为 UI 图标（应使用 Style D WXSS base64 SVG）。
2. ``NO_TEXT_INLINE_FLEX``：禁止在 ``<text>`` 上绑定含 ``display: inline-flex`` 的样式类（规避微信 v2 竖向拉伸留白）。
3. ``BUTTON_EXPLICIT_WIDTH``：页面/组件中的所有 ``<button>`` 必须具备显式声明了 ``width:`` 的样式类或内联宽度（防 v2 默认 184px 溢出）。
4. ``NO_NAVIGATE_TO_TABBAR``：禁止使用 ``wx.navigateTo`` 跳转到 TabBar 页面（应使用 ``wx.switchTab``）。
5. ``USER_COPY_HYGIENE``：禁止向用户展示 ``收摊`` 措辞、模板插值 ``${...errorCode...}``、或在 ``wx.showModal`` 中拼接 ``JSON.stringify``。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MINIPROGRAM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MINIPROGRAM_ROOT.parent
SCAN_DIRS = ("pages", "components", "services", "utils")
UI_DIRS = ("pages", "components")
SKIP_DIRS = frozenset({"node_modules", "miniprogram_npm", ".git", "dist", "build", "scripts"})

EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
INLINE_FLEX_RE = re.compile(r"display\s*:\s*inline-flex\b", re.IGNORECASE)
WIDTH_PROP_RE = re.compile(r"(?:^|[;\s{])width\s*:", re.IGNORECASE)
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
CLASS_SELECTOR_RE = re.compile(r"\.([a-zA-Z0-9_-]+)")
MUSTACHE_RE = re.compile(r"\{\{([\s\S]*?)\}\}")
QUOTED_STR_RE = re.compile(r"""['"]([a-zA-Z0-9_-]+)['"]""")
TAG_TEXT_RE = re.compile(r"<text\b([^>]*)>", re.DOTALL)
TAG_BUTTON_RE = re.compile(r"<button\b([^>]*)>", re.DOTALL)
ATTR_CLASS_RE = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)
ATTR_STYLE_RE = re.compile(r"""\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)
ERROR_CODE_INTERP_RE = re.compile(r"\$\{[^}]*\berrorCode\b[^}]*\}")
SHOW_MODAL_JSON_RE = re.compile(r"wx\.showModal\([\s\S]{0,300}JSON\.stringify")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        rel = self.path.relative_to(REPO_ROOT).as_posix()
        return f"{rel}:{self.line}: [{self.rule}] {self.detail}"


def _preserve_newlines(text: str) -> str:
    return "\n" * text.count("\n")


def strip_ts_comments(source: str) -> str:
    """剥离 JS/TS 注释，保留换行符以维持行号准确。"""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in ('"', "'", "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                cur = source[i]
                out.append(cur)
                if cur == "\\" and i + 1 < n:
                    out.append(source[i + 1])
                    i += 2
                    continue
                i += 1
                if cur == quote:
                    break
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            start = i
            while i < n and source[i] != "\n":
                i += 1
            out.append(_preserve_newlines(source[start:i]))
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(_preserve_newlines(source[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def strip_wxml_comments(source: str) -> str:
    """剥离 WXML 注释，保留换行符以维持行号准确。"""
    return re.sub(r"<!--[\s\S]*?-->", lambda m: _preserve_newlines(m.group(0)), source)


def strip_css_comments(source: str) -> str:
    """剥离 WXSS/CSS 块注释，保留换行符。"""
    return re.sub(r"/\*[\s\S]*?\*/", lambda m: _preserve_newlines(m.group(0)), source)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def extract_class_tokens(class_attr: str) -> set[str]:
    """从 class 属性中提取静态类名及插值三元表达式中的候选类名。"""
    tokens: set[str] = set()
    static_part = MUSTACHE_RE.sub(" ", class_attr)
    for tok in static_part.split():
        if re.fullmatch(r"[a-zA-Z0-9_-]+", tok):
            tokens.add(tok)
    for m in MUSTACHE_RE.finditer(class_attr):
        for q in QUOTED_STR_RE.finditer(m.group(1)):
            for tok in q.group(1).split():
                if re.fullmatch(r"[a-zA-Z0-9_-]+", tok):
                    tokens.add(tok)
    return tokens


def parse_wxss_classes(wxss_path: Path) -> tuple[set[str], set[str]]:
    """解析 WXSS 文件中的 inline-flex 类与显式 width 类。"""
    if not wxss_path.is_file():
        return set(), set()
    css = strip_css_comments(wxss_path.read_text(encoding="utf-8"))
    inline_flex_cls: set[str] = set()
    width_cls: set[str] = set()
    for match in CSS_RULE_RE.finditer(css):
        selectors, body = match.group(1), match.group(2)
        if INLINE_FLEX_RE.search(body):
            for sel in selectors.split(","):
                for cm in CLASS_SELECTOR_RE.finditer(sel):
                    inline_flex_cls.add(cm.group(1))
        if WIDTH_PROP_RE.search(body):
            for sel in selectors.split(","):
                # 提取完整选择器链末段或所有相关类名（如 .subs .sub / button.cta）
                parts = sel.strip().split()
                last = parts[-1] if parts else sel
                for cm in CLASS_SELECTOR_RE.finditer(last):
                    width_cls.add(cm.group(1))
    return inline_flex_cls, width_cls


def iter_files(sub_dirs: tuple[str, ...], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for sub in sub_dirs:
        base = MINIPROGRAM_ROOT / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if SKIP_DIRS & set(p.parts):
                continue
            if p.suffix in suffixes:
                files.append(p)
    return files


def load_tabbar_pages() -> list[str]:
    app_json = json.loads((MINIPROGRAM_ROOT / "app.json").read_text(encoding="utf-8"))
    tabs = app_json.get("tabBar", {}).get("list", [])
    pages = [str(item.get("pagePath", "")).strip() for item in tabs if item.get("pagePath")]
    return pages or [
        "pages/index/index",
        "pages/goods/index",
        "pages/debts/index",
        "pages/mine/index",
    ]


def check_all() -> list[Violation]:
    violations: list[Violation] = []
    global_inline_flex, global_width = parse_wxss_classes(MINIPROGRAM_ROOT / "app.wxss")
    tabbar_pages = load_tabbar_pages()

    # 1. WXML 检查：Emoji / <text> + inline-flex / <button> width / 收摊文案
    for wxml in iter_files(UI_DIRS, (".wxml",)):
        raw = wxml.read_text(encoding="utf-8")
        cleaned = strip_wxml_comments(raw)
        local_inline_flex, local_width = parse_wxss_classes(wxml.with_suffix(".wxss"))
        inline_flex_set = global_inline_flex | local_inline_flex
        width_set = global_width | local_width

        for m in EMOJI_RE.finditer(cleaned):
            violations.append(
                Violation(
                    wxml,
                    line_of(cleaned, m.start()),
                    "STYLE_D_NO_EMOJI",
                    f"WXML 出现系统 Emoji `{m.group(0)}`，请改用 Style D SVG 图标",
                )
            )

        if "收摊" in cleaned:
            idx = cleaned.find("收摊")
            violations.append(
                Violation(
                    wxml,
                    line_of(cleaned, idx),
                    "USER_COPY_HYGIENE",
                    "WXML 出现禁用措辞「收摊」",
                )
            )

        for m in TAG_TEXT_RE.finditer(cleaned):
            attrs = m.group(1)
            cm = ATTR_CLASS_RE.search(attrs)
            if not cm:
                continue
            cls_val = cm.group(1) if cm.group(1) is not None else cm.group(2)
            hits = sorted(extract_class_tokens(cls_val) & inline_flex_set)
            if hits:
                violations.append(
                    Violation(
                        wxml,
                        line_of(cleaned, m.start()),
                        "NO_TEXT_INLINE_FLEX",
                        f"<text> 使用了 display: inline-flex 类 {hits}，请改为 <view>",
                    )
                )

        for m in TAG_BUTTON_RE.finditer(cleaned):
            attrs = m.group(1)
            sm = ATTR_STYLE_RE.search(attrs)
            style_val = (sm.group(1) if sm.group(1) is not None else sm.group(2)) if sm else ""
            if WIDTH_PROP_RE.search(style_val):
                continue
            cm = ATTR_CLASS_RE.search(attrs)
            cls_val = (cm.group(1) if cm.group(1) is not None else cm.group(2)) if cm else ""
            tokens = extract_class_tokens(cls_val)
            if not (tokens & width_set):
                violations.append(
                    Violation(
                        wxml,
                        line_of(cleaned, m.start()),
                        "BUTTON_EXPLICIT_WIDTH",
                        f"<button class=\"{cls_val.strip()}\"> 未匹配到显式 width 样式，存在 v2 默认 184px 溢出风险",
                    )
                )

    # 2. TS 检查：Emoji / navigateTo TabBar / errorCode 插值 / showModal + JSON.stringify / 收摊文案
    for ts in iter_files(SCAN_DIRS, (".ts",)):
        raw = ts.read_text(encoding="utf-8")
        cleaned = strip_ts_comments(raw)
        is_ui = ts.parts and any(d in ts.relative_to(MINIPROGRAM_ROOT).parts[:1] for d in UI_DIRS)

        if is_ui:
            for m in EMOJI_RE.finditer(cleaned):
                violations.append(
                    Violation(
                        ts,
                        line_of(cleaned, m.start()),
                        "STYLE_D_NO_EMOJI",
                        f"TS 界面文案出现系统 Emoji `{m.group(0)}`",
                    )
                )

        if "收摊" in cleaned:
            idx = cleaned.find("收摊")
            violations.append(
                Violation(
                    ts,
                    line_of(cleaned, idx),
                    "USER_COPY_HYGIENE",
                    "TS 出现禁用措辞「收摊」",
                )
            )

        if "pages" in ts.relative_to(MINIPROGRAM_ROOT).parts[:1]:
            for m in ERROR_CODE_INTERP_RE.finditer(cleaned):
                violations.append(
                    Violation(
                        ts,
                        line_of(cleaned, m.start()),
                        "USER_COPY_HYGIENE",
                        f"页面提示插值了内部错误码 `{m.group(0)}`",
                    )
                )

        for m in SHOW_MODAL_JSON_RE.finditer(cleaned):
            violations.append(
                Violation(
                    ts,
                    line_of(cleaned, m.start()),
                    "USER_COPY_HYGIENE",
                    "wx.showModal 弹窗内容禁止拼接 JSON.stringify",
                )
            )

        for lineno, line in enumerate(cleaned.splitlines(), 1):
            if "navigateTo" in line:
                for tp in tabbar_pages:
                    if tp in line:
                        violations.append(
                            Violation(
                                ts,
                                lineno,
                                "NO_NAVIGATE_TO_TABBAR",
                                f"禁止用 wx.navigateTo 跳转 TabBar 页面 `{tp}`，请改用 wx.switchTab",
                            )
                        )

    return violations


def main() -> int:
    violations = check_all()
    if violations:
        for v in violations:
            print(v.format(), file=sys.stderr)
        print(f"UI 规则巡检失败：共发现 {len(violations)} 处违规", file=sys.stderr)
        return 1
    print(f"UI 规则巡检通过（已扫描 {MINIPROGRAM_ROOT} 全部页面、组件与服务，0 违规）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
