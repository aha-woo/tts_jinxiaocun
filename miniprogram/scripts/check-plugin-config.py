#!/usr/bin/env python3
"""校验小程序 ``app.json`` 的插件配置与 mp 后台是否可能对不上。

为什么需要这个脚本
------------------
``app.json`` 里 ``plugins.WechatSI.version`` **必须**与 mp 后台「设置 → 第三方服务 →
插件管理 → 详情」里显示的版本号**完全一致**。写错的后果是：

- **编译期不报错**（JSON 合法）；
- **运行时才报**「插件不存在」，而且只在真机上出现；
- 用户看到的现象是「按大按钮一点反应都没有」，**完全不知道是版本号写错**。

版本号是**平台事实**，会随插件发版变化；本项目把它写死在 ``app.json`` 里，
所以需要有人（或 CI）在改之前核对一眼。本脚本做三件事：

1. 确认 ``app.json`` 里插件的 ``provider`` 是微信同声传译的官方 AppID；
2. 确认 ``version`` 是一个**具体版本号**（不是 ``latest``——那会直接报「插件不存在」）；
3. 确认 ``permission.scope.record.desc`` 非空且 ≤30 字符
   （官方限制；写成空串的话真机会拒绝拉起授权弹窗）。

另外把「后台详情页的版本号」通过环境变量 ``WECHATSI_VERSION`` 传进来时，
会与 ``app.json`` 逐字比对——这是**唯一**能自动抓到版本号不一致的办法
（后台没有开放 API 读这个值，只能人工看一眼再传进来）。

用法::

    python miniprogram/scripts/check-plugin-config.py
    WECHATSI_VERSION=0.3.5 python miniprogram/scripts/check-plugin-config.py

退出码：0 = 通过；1 = 有问题（把原因打出来）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: 微信同声传译插件的官方 AppID（用户已于 2026-11-17 提供）
EXPECTED_PROVIDER = "wx069ba97219f66d99"

#: ``permission.scope.record.desc`` 的官方长度上限
MAX_DESC_LENGTH = 30

#: ``version`` 不能是这些值（微信不会解析，会直接报「插件不存在」）
INVALID_VERSIONS = {"latest", "*", "", "0"}


def main() -> int:
    app_json = Path(__file__).resolve().parent.parent / "app.json"
    if not app_json.is_file():
        print(f"✗ 找不到 {app_json}", file=sys.stderr)
        return 1

    try:
        config = json.loads(app_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"✗ app.json 不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    problems: list[str] = []

    plugins = config.get("plugins")
    if not isinstance(plugins, dict) or "WechatSI" not in plugins:
        print("✗ app.json 里没有 plugins.WechatSI —— 语音功能不可能工作")
        return 1

    si = plugins["WechatSI"]
    version = str(si.get("version", ""))
    provider = str(si.get("provider", ""))

    if provider != EXPECTED_PROVIDER:
        problems.append(
            f"plugins.WechatSI.provider 应为 {EXPECTED_PROVIDER}，实际是 {provider!r}"
        )

    if version.lower() in INVALID_VERSIONS:
        problems.append(
            f"plugins.WechatSI.version 是 {version!r} —— 微信不解析这种值，"
            "运行时会直接报「插件不存在」。必须写具体版本号"
        )

    # 与后台核对（人工把「详情」页的版本号传进来）
    expected = os.environ.get("WECHATSI_VERSION", "").strip()
    if expected and expected != version:
        problems.append(
            f"版本号对不上：app.json 写的是 {version!r}，"
            f"而后台详情页显示的是 {expected!r} —— 真机上会报「插件不存在」"
        )

    permission = config.get("permission")
    record = (permission or {}).get("scope.record") if isinstance(permission, dict) else None
    if not isinstance(record, dict):
        problems.append("app.json 里缺 permission.scope.record —— 麦克风授权弹窗没有用途说明")
    else:
        desc = str(record.get("desc", ""))
        if desc.strip() == "":
            problems.append("permission.scope.record.desc 是空串 —— 真机会拒绝拉起授权弹窗")
        elif len(desc) > MAX_DESC_LENGTH:
            problems.append(
                f"permission.scope.record.desc 有 {len(desc)} 字，官方上限 {MAX_DESC_LENGTH} 字"
            )

    if problems:
        print("✗ 插件配置检查未通过：")
        for item in problems:
            print(f"  - {item}")
        print()
        print("  怎么核对后台版本号：mp 后台 → 设置 → 第三方服务 → 插件管理 →")
        print("  「微信同声传译」→ 详情 → 版本号。然后：")
        print("  WECHATSI_VERSION=<那个版本号> python miniprogram/scripts/check-plugin-config.py")
        return 1

    print(f"✓ 插件配置 OK：provider={provider} version={version}")
    if not expected:
        print("  （未设置 WECHATSI_VERSION，跳过与后台的版本号比对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
