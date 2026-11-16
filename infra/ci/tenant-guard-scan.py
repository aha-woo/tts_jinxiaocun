#!/usr/bin/env python3
"""租户守卫扫描（CI 静态检查）：发现「不带 tenant_id 的裸 SQL」。

背景
----
架构 D7 / §4.2：多租户隔离靠「RLS（DB 强制）+ ORM 全局 scope + CI 静态扫描」多重防线。
本脚本是**最后一道**：任何原生 SQL 只要没有 ``tenant_id`` 条件，就可能是跨租户越权。

判定规则
--------
1. 用 stdlib ``tokenize`` 抽取 Python 源码里的 SQL 文本，两类都要抓：
   - 普通字符串字面量（``STRING``）；
   - **f-string**（``FSTRING_START``/``FSTRING_MIDDLE``/``FSTRING_END``）：
     拼接后把 ``{...}`` 插值替换成 ``?``。**只扫 STRING 会漏掉 f-string 拼出来的 SQL，
     这是上一版脚本的真实盲区**（``f"ALTER TABLE {name} ..."`` 当时完全不可见）。
2. 文本里出现 SQL 关键字（SELECT / INSERT INTO / UPDATE ... SET / DELETE FROM /
   ALTER TABLE / CREATE TABLE / DROP TABLE / TRUNCATE）；
3. 且该文本中**没有** ``tenant_id`` → 记为命中。

结构豁免（不是业务数据访问，逐条列明理由，不做「整文件放行」）
--------------------------------------------------------------
- ``SELECT 1``：连通性探测；
- ``SELECT set_config(...)``：设置会话变量本身（不是查业务表）；
- ``ALTER TABLE ... ENABLE|FORCE ROW LEVEL SECURITY``：RLS 开关语句；
- ``CREATE POLICY`` / ``DROP POLICY``：策略定义本身就是在描述隔离条件，
  天然不带 ``WHERE tenant_id``（它写的是 ``USING (...)``）。

局限（如实声明，不假装能全覆盖）
------------------------------
- 只查原生 SQL 文本；SQLAlchemy ORM 的 ``select(Model)`` 是否带 tenant scope 由
  ``TenantRepository`` + ``tests/test_tenant_scope_no_rls.py`` 的跨租户单测保证；
- 拼接 SQL（f-string / ``+`` / ``%``）也会被扫到，只要片段里缺 tenant_id 就报——刻意从严。

用法
----
    python infra/ci/tenant-guard-scan.py [扫描目录...] [--json] [--quiet] [--show-exempt]

退出码：0 = 无命中；1 = 有命中（CI 直接失败）。
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path

SQL_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"SELECT\s|"
    r"INSERT\s+INTO\b|"
    r"UPDATE\s+\w+\s+SET\b|"
    r"DELETE\s+FROM\b|"
    r"ALTER\s+TABLE\b|"
    r"CREATE\s+TABLE\b|"
    r"DROP\s+TABLE\b|"
    r"TRUNCATE\b"
    r")",
    re.IGNORECASE,
)

TENANT_PATTERN = re.compile(r"tenant_id", re.IGNORECASE)

#: 结构豁免：命中这些形态的语句与「租户数据访问」无关（理由见模块 docstring）
STRUCTURAL_EXEMPTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*SELECT\s+1\s*;?\s*$", re.IGNORECASE), "连通性探测 SELECT 1"),
    (re.compile(r"\bSELECT\s+set_config\s*\(", re.IGNORECASE), "设置会话变量（非业务表查询）"),
    (
        re.compile(
            r"\bALTER\s+TABLE\b.*\b(ENABLE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "RLS 开关语句",
    ),
    (re.compile(r"\bCREATE\s+POLICY\b", re.IGNORECASE), "策略定义本身即隔离条件"),
    (re.compile(r"\bDROP\s+POLICY\b", re.IGNORECASE), "策略删除"),
)

#: 默认扫描根目录（相对仓库根）：业务代码 + 迁移脚本（迁移里的裸 SQL 同样能越权）
DEFAULT_TARGETS = ("server/app", "server/migrations")

#: 汇总里要单独点名的目录（证明「模型层也被扫到了」）
SPOTLIGHT_DIRS = (
    "server/app/models",
    "server/app/voice",
    "server/app/services",
    "server/app/domain",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

FSTRING_START = getattr(tokenize, "FSTRING_START", -1)
FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", -1)
FSTRING_END = getattr(tokenize, "FSTRING_END", -1)


@dataclass(frozen=True)
class Finding:
    """一条命中记录。"""

    file: str
    line: int
    column: int
    snippet: str


@dataclass(frozen=True)
class Exemption:
    """一条结构豁免记录（供 ``--show-exempt`` 复核）。"""

    file: str
    line: int
    reason: str
    snippet: str


def iter_python_files(target: Path) -> list[Path]:
    """列出目标下的所有 .py 文件（目标本身是文件时直接返回）。"""
    if target.is_file():
        return [target] if target.suffix == ".py" else []
    if not target.exists():
        return []
    return sorted(path for path in target.rglob("*.py") if path.is_file())


def unquote(literal: str) -> str:
    """把字符串字面量的引号去掉，得到真实文本（失败时原样返回）。

    必须这么做，否则结构豁免（如 ``SELECT 1``）会被外层引号干扰而匹配不上。
    """
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return literal
    return value if isinstance(value, str) else literal


def collect_sql_texts(tokens: list[tokenize.TokenInfo]) -> list[tuple[int, int, str]]:
    """抽取所有可能的 SQL 文本：普通字符串 + **f-string 拼接串**。

    Returns:
        ``[(行号, 列号, 文本), ...]``；f-string 的插值部分替换成 ``?``。
    """
    collected: list[tuple[int, int, str]] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token.type == tokenize.STRING:
            collected.append((token.start[0], token.start[1] + 1, unquote(token.string)))
        elif token.type == FSTRING_START:
            start = token.start
            parts: list[str] = []
            depth = 1
            index += 1
            while index < total and depth > 0:
                current = tokens[index]
                if current.type == FSTRING_START:
                    depth += 1
                elif current.type == FSTRING_END:
                    depth -= 1
                elif depth == 1 and current.type == FSTRING_MIDDLE:
                    parts.append(current.string)
                elif depth == 1 and current.type == tokenize.OP and current.string == "{":
                    # 进入插值表达式：用一个占位符代表「动态拼进来的那一段」
                    parts.append("?")
                index += 1
            collected.append((start[0], start[1] + 1, "".join(parts)))
            continue
        index += 1
    return collected


def exemption_reason(text: str) -> str | None:
    """命中结构豁免则返回理由，否则 None。"""
    for pattern, reason in STRUCTURAL_EXEMPTIONS:
        if pattern.search(text):
            return reason
    return None


def scan_file(path: Path) -> tuple[list[Finding], list[Exemption]]:
    """扫描单个 Python 文件里的 SQL 文本。"""
    findings: list[Finding] = []
    exemptions: list[Exemption] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:  # pragma: no cover - 读不到就报警而非静默跳过
        print(f"[tenant-guard] 无法读取 {path}: {exc}", file=sys.stderr)
        return findings, exemptions

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError as exc:  # pragma: no cover - 语法错误应由 lint 兜住
        print(f"[tenant-guard] 无法解析 {path}: {exc}", file=sys.stderr)
        return findings, exemptions

    relative = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)

    for line, column, text in collect_sql_texts(tokens):
        if not SQL_KEYWORD_PATTERN.search(text):
            continue
        snippet = " ".join(text.split())
        if len(snippet) > 160:
            snippet = f"{snippet[:157]}..."
        if TENANT_PATTERN.search(text):
            continue
        reason = exemption_reason(text)
        if reason is not None:
            exemptions.append(Exemption(file=relative, line=line, reason=reason, snippet=snippet))
            continue
        findings.append(Finding(file=relative, line=line, column=column, snippet=snippet))
    return findings, exemptions


def scan_targets(targets: list[Path]) -> tuple[list[Finding], list[Exemption], list[Path]]:
    """扫描多个目标路径，返回 (命中, 豁免, 扫描到的文件)。"""
    findings: list[Finding] = []
    exemptions: list[Exemption] = []
    files: list[Path] = []
    for target in targets:
        for file_path in iter_python_files(target):
            files.append(file_path)
            file_findings, file_exemptions = scan_file(file_path)
            findings.extend(file_findings)
            exemptions.extend(file_exemptions)
    return findings, exemptions, files


def _count_scanned_in(directory: Path, files: list[Path]) -> int:
    """统计某目录下被扫描到的文件数（用于证明模型层确实被覆盖）。"""
    try:
        resolved = directory.resolve()
    except OSError:  # pragma: no cover
        return 0
    return sum(1 for path in files if path.resolve().is_relative_to(resolved))


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="扫描不带 tenant_id 的裸 SQL（多租户守卫）")
    parser.add_argument(
        "targets",
        nargs="*",
        help=f"扫描目录或文件，默认 {', '.join(DEFAULT_TARGETS)}",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（CI 归档用）")
    parser.add_argument("--quiet", action="store_true", help="只在有命中时输出")
    parser.add_argument("--show-exempt", action="store_true", help="列出结构豁免明细（复核用）")
    args = parser.parse_args(argv)

    raw_targets = args.targets or list(DEFAULT_TARGETS)
    targets = [Path(item) if Path(item).is_absolute() else REPO_ROOT / item for item in raw_targets]

    findings, exemptions, scanned_files = scan_targets(targets)
    coverage = {
        name: _count_scanned_in(REPO_ROOT / name, scanned_files) for name in SPOTLIGHT_DIRS
    }

    if args.json:
        print(
            json.dumps(
                {
                    "scanned_files": len(scanned_files),
                    "coverage": coverage,
                    "hits": len(findings),
                    "exemptions": len(exemptions),
                    "findings": [asdict(item) for item in findings],
                    "exempt": [asdict(item) for item in exemptions],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif not args.quiet or findings:
        detail = "，".join(f"{name} {count} 个" for name, count in coverage.items())
        print(
            f"[tenant-guard] 扫描 {len(scanned_files)} 个 Python 文件"
            f"（{detail}），命中 {len(findings)} 处，结构豁免 {len(exemptions)} 处"
        )
        for item in findings:
            print(f"  - {item.file}:{item.line}:{item.column}  {item.snippet}")
        if args.show_exempt:
            for item in exemptions:
                print(f"  ~ 豁免 {item.file}:{item.line}  [{item.reason}]  {item.snippet}")

    if findings:
        print(
            "[tenant-guard] 失败：以上 SQL 未包含 tenant_id 条件。"
            "请改用 TenantRepository（自动 scope），或在 SQL 中显式加 tenant_id 过滤。",
            file=sys.stderr,
        )
        return 1

    for name, count in coverage.items():
        if count == 0:
            print(
                f"[tenant-guard] 警告：没有扫描到 {name} 下的任何文件，覆盖范围可能不对",
                file=sys.stderr,
            )

    if not args.json and not args.quiet:
        print("[tenant-guard] 通过：未发现缺 tenant_id 的裸 SQL。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
