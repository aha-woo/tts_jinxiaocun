#!/usr/bin/env python
"""模板层编译检查：用微信开发者工具自带的官方编译器逐个编译 WXML / WXSS。

为什么需要它
------------
`tsc --noEmit` 只能证明 **TS 类型与语法**没问题，**不能**证明 WXML/WXSS 能编译。
模板层的错误（标签没闭合、`wx:for` 用错、WXSS 少个括号）只会在真机/模拟器里炸，
而这类错误恰恰是最容易在「改完逻辑顺手改个标签」时引入的。

`cli.bat open` 需要开发者在 IDE 里手动开「服务端口」，非交互环境里跑不了。
好在开发者工具包里自带官方编译器，命令行可直接调用，**不需要开 IDE**：

    <微信开发者工具>/resources/app.asar.unpacked/node_modules/wcc-exec/wcc.exe   # WXML
    <微信开发者工具>/resources/app.asar.unpacked/node_modules/wcc-exec/wcsc.exe  # WXSS

判定口径
--------
这两个编译器**退出码永远是 0**，所以不能用 returncode 判断成败。
唯一可靠的信号是**输出是否为空**：任一文件输出非空 = 编译失败。

用法
----
    python miniprogram/scripts/check-templates.py            # 走默认路径探测
    python miniprogram/scripts/check-templates.py --wcc D:/x/wcc.exe --wcsc D:/x/wcsc.exe
    WCC=/path/to/wcc WCSC=/path/to/wcsc python miniprogram/scripts/check-templates.py

找不到编译器时**明确打印 skip 并以 0 退出**（不静默当通过），因为：
- 没有编译器 ≠ 模板没问题，只是「这一层没验」；
- 但也不该让没装工具的人无法跑其他检查。

⚠️ 平台限制（**如实标注，别让 CI 绿了骗人**）
--------------------------------------------
`wcc.exe` / `wcsc.exe` 是 **Windows PE 可执行文件**，GitHub Actions 的 Linux runner
上既没有也不需要（也没有对应的 Linux 版命令行编译器）。
**所以「CI 全绿」不代表模板层验过**——这条只能在装了微信开发者工具的 Windows 本地跑。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: 小程序源码根（本文件在 ``miniprogram/scripts/`` 下）
MINIPROGRAM_ROOT = Path(__file__).resolve().parents[1]

#: 微信开发者工具默认安装位置（Windows）
DEFAULT_DEVTOOLS_CANDIDATES: tuple[Path, ...] = (
    Path("D:/wechatSDK"),
    Path("C:/Program Files (x86)/Tencent/微信web开发者工具"),
    Path("C:/Program Files/Tencent/微信web开发者工具"),
)

#: 编译器相对开发者工具根目录的路径
_WCC_RELATIVE = Path("resources/app.asar.unpacked/node_modules/wcc-exec/wcc.exe")
_WCSC_RELATIVE = Path("resources/app.asar.unpacked/node_modules/wcc-exec/wcsc.exe")

#: 遍历时跳过的目录（第三方依赖与构建产物不算我们写的模板）
_SKIP_DIRS = frozenset({"node_modules", "miniprogram_npm", ".git", "dist", "build"})


def find_tool(explicit: str | None, env_key: str, relative: Path) -> Path | None:
    """按「显式参数 → 环境变量 → 默认安装位置 → PATH」的顺序找编译器。

    Args:
        explicit: 命令行传入的路径（最高优先级）。
        env_key: 环境变量名（如 ``WCC``）。
        relative: 相对开发者工具根目录的路径。

    Returns:
        可执行文件路径；找不到返回 ``None``。
    """
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None

    from_env = os.environ.get(env_key)
    if from_env:
        candidate = Path(from_env)
        if candidate.is_file():
            return candidate

    for root in DEFAULT_DEVTOOLS_CANDIDATES:
        candidate = root / relative
        if candidate.is_file():
            return candidate

    # 最后试一次 PATH（有人手工把它加进 PATH 了）
    found = shutil.which(relative.name)
    return Path(found) if found else None


def iter_templates(root: Path) -> tuple[list[Path], list[Path]]:
    """列出全部 ``*.wxml`` 与 ``*.wxss``（跳过 ``node_modules`` 等）。

    Returns:
        ``(wxml_files, wxss_files)``，都按路径排序，保证输出稳定可 diff。
    """
    wxml: list[Path] = []
    wxss: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _SKIP_DIRS & set(path.parts):
            continue
        if path.suffix == ".wxml":
            wxml.append(path)
        elif path.suffix == ".wxss":
            wxss.append(path)
    return wxml, wxss


def compile_one(tool: Path, target: Path, *, extra_args: list[str]) -> str:
    """编译单个文件，返回其输出（**空串 = 通过**）。

    Args:
        tool: 编译器路径。
        target: 待编译文件。
        extra_args: 编译器开关（wcc 需要 ``-d -o <null>``，wcsc 只需 ``-o <null>``）。

    Returns:
        stdout 与 stderr 合并后的文本（已 strip）。
    """
    completed = subprocess.run(  # noqa: S603 - 命令由本脚本拼装，参数是本地文件路径
        [str(tool), *extra_args, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # 这两个编译器退出码恒为 0，成败只能看输出
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    return combined


def check_group(
    tool: Path,
    files: list[Path],
    *,
    extra_args: list[str],
    label: str,
) -> list[tuple[Path, str]]:
    """编译一组文件，返回失败列表。

    Args:
        tool: 编译器路径。
        files: 待编译文件。
        extra_args: 编译器开关。
        label: 展示用名称（``WXML`` / ``WXSS``）。

    Returns:
        ``[(文件路径, 编译输出), ...]``；空列表表示全绿。
    """
    print(f"\n=== {label} {len(files)} 个 ===")
    failures: list[tuple[Path, str]] = []
    for target in files:
        output = compile_one(tool, target, extra_args=extra_args)
        relative = target.relative_to(MINIPROGRAM_ROOT).as_posix()
        if output == "":
            print(f"[OK] {relative}")
            continue
        print(f"[FAIL] {relative}")
        for line in output.splitlines():
            print(f"       {line}")
        failures.append((target, output))
    print(f"失败数={len(failures)}")
    return failures


def main(argv: list[str] | None = None) -> int:
    """入口。

    Returns:
        进程退出码：全绿或明确 skip 返回 0；有编译失败返回 1。
    """
    parser = argparse.ArgumentParser(description="用官方 wcc/wcsc 编译检查 WXML / WXSS")
    parser.add_argument("--wcc", default=None, help="wcc.exe 路径（WXML 编译器）")
    parser.add_argument("--wcsc", default=None, help="wcsc.exe 路径（WXSS 编译器）")
    parser.add_argument(
        "--root",
        default=str(MINIPROGRAM_ROOT),
        help="小程序源码根目录（默认本脚本上一级）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"[error] 找不到小程序目录：{root}", file=sys.stderr)
        return 1

    wcc = find_tool(args.wcc, "WCC", _WCC_RELATIVE)
    wcsc = find_tool(args.wcsc, "WCSC", _WCSC_RELATIVE)
    if wcc is None or wcsc is None:
        missing = [name for name, tool in (("wcc.exe", wcc), ("wcsc.exe", wcsc)) if tool is None]
        print("[skip] 未找到微信官方模板编译器：" + "、".join(missing))
        print("       这不是「模板没问题」，而是「模板层这一关没验」。")
        print("       装好微信开发者工具后重试，或用 --wcc / --wcsc 显式指定路径：")
        print("         微信开发者工具/resources/app.asar.unpacked/node_modules/wcc-exec/wcc.exe")
        print("         微信开发者工具/resources/app.asar.unpacked/node_modules/wcc-exec/wcsc.exe")
        print("       注意：这两个是 Windows 可执行文件，Linux CI runner 上没有，")
        print("            所以「CI 全绿」不代表模板层验过。")
        return 0

    wxml_files, wxss_files = iter_templates(root)
    if not wxml_files and not wxss_files:
        print(f"[warn] {root} 下没有找到任何 wxml/wxss", file=sys.stderr)
        return 0

    # wcc 需要 `-d`（把结果打到 stdout）+ `-o` 指定输出；wcsc 只接受 `-o`
    null_sink = os.devnull
    failures = check_group(wcc, wxml_files, extra_args=["-d", "-o", null_sink], label="WXML")
    failures += check_group(wcsc, wxss_files, extra_args=["-o", null_sink], label="WXSS")

    total = len(wxml_files) + len(wxss_files)
    print("\n" + "=" * 60)
    if failures:
        print(f"模板编译失败 {len(failures)} / {total} 个文件")
        return 1
    print(f"模板编译全部通过：WXML {len(wxml_files)} + WXSS {len(wxss_files)} = {total} 个文件，零错误")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
