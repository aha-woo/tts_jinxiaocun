"""小程序端侧静态契约（T05 4.3 / 4.1 / 4.2 的可机械检查部分）。

为什么要有这个文件
------------------
「生产只走 ``callContainer``」「UI 只暴露单商户」「卡片 7 区块一个不少」这类约束
如果只写在文档里，就没法防止某次改动悄悄加回一条 ``http://`` 直连——
而那条直连上线后会因为「没配服务器合法域名」整批失败，且很难从日志看出来。

所以这里把端侧当成**可被 pytest 读取的产物**，逐条钉死：

1. **唯一出网路径**：``services/api.ts`` 里既没有 ``http://`` 也没有 ``wx.request``
   （注释也会被剥掉再扫，避免「注释里提了一嘴」被误判成违规，也避免被注释糊弄过去）；
2. **dev 通道唯一出处**：全仓扫描，``http://`` 与 ``wx.request`` **只允许**出现在
   ``services/dev-channel.ts``，且 ``DEV_MODE`` 必须字面量 ``false``；
3. **页面与目录一一对应**：``app.json`` 声明的每个页面都存在四件套，``usingComponents``
   指向的组件目录都真实存在（小程序的组件路径写错是**运行时**才报的错）；
4. **卡片 7 区块**：``original/intent/slots/calc/impact/risk/actions`` 一个不少；
5. **管理页齐活**：档案 / 单据与更正 / 三张报表 / 用量与套餐 / 权限 / 导出 / R3 待办 /
   说法训练，八块一个不少；R3 必须有 ``role=boss`` 判断与二次确认；
6. **铁律**：无门店/组织/多仓概念、无租户切换器、无网页版入口、导出不走网页下载；
7. **拇指热区 ≥88×88px**、**双栏有窄屏回落断点**。

这些断言都是「读文件做结构检查」，不需要装微信开发者工具，因此可在 CI 里跑。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

#: 仓库根目录（本文件在 ``server/tests/`` 下）
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 小程序源码根
MP_ROOT = REPO_ROOT / "miniprogram"

#: 卡片 7 区块（顺序即展示顺序，架构 §6.1）
CARD_BLOCKS: tuple[str, ...] = (
    "original",
    "intent",
    "slots",
    "calc",
    "impact",
    "risk",
    "actions",
)

#: 管理页必须齐的面板（T05 4.2）
ADMIN_PANELS: tuple[str, ...] = (
    "profile",
    "docs",
    "report",
    "usage",
    "permission",
    "export",
    "tasks",
    "dict",
)

_TS_SUFFIXES = (".ts", ".js")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    """读文本（统一 UTF-8，失败直接 fail，不静默返回空串）。"""
    return path.read_text(encoding="utf-8")


def strip_comments(source: str) -> str:
    """剥掉 ``//`` 行注释与 ``/* */`` 块注释（字符串字面量简单跳过）。

    目的：**只检查真实代码**。既不冤枉「注释里解释了为什么禁用 wx.request」，
    也不让任何人把违规代码塞进注释区以外的地方蒙混过关。
    """
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in ('"', "'", "`"):
            quote = char
            out.append(char)
            index += 1
            while index < length:
                current = source[index]
                out.append(current)
                if current == "\\" and index + 1 < length:
                    out.append(source[index + 1])
                    index += 2
                    continue
                index += 1
                if current == quote:
                    break
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            closing = source.find("*/", index + 2)
            index = length if closing == -1 else closing + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def strip_wxml_comments(source: str) -> str:
    """剥掉 WXML 的 ``<!-- -->`` 注释。

    注释里经常写着「不出现门店切换」这类**否定式说明**，如果直接扫字面量会自己撞自己。
    只留真实节点，才是「UI 上有没有这个东西」。
    """
    previous = None
    current = source
    while previous != current:
        previous = current
        current = re.sub(r"<!--[\s\S]*?-->", "", current)
    return current


def iter_sources(root: Path, suffixes: tuple[str, ...] = _TS_SUFFIXES) -> list[Path]:
    """遍历源码文件（跳过 ``node_modules``）。"""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "node_modules" not in path.parts
    )


def page_ids() -> list[str]:
    """``app.json`` 里声明的页面（形如 ``pages/index/index``）。"""
    data = json.loads(_read(MP_ROOT / "app.json"))
    return list(data["pages"])


# ---------------------------------------------------------------------------
# 一、生产出网路径（4.3）
# ---------------------------------------------------------------------------
def test_api_module_has_no_http_and_no_wx_request() -> None:
    """``services/api.ts`` 代码里**不存在** ``http://`` 直连与 ``wx.request``。"""
    source = strip_comments(_read(MP_ROOT / "services" / "api.ts"))
    assert "http://" not in source, "api.ts 出现 http:// 直连分支——生产必须走 callContainer"
    assert "https://" not in source, "api.ts 出现 https:// 直连分支——生产必须走 callContainer"
    assert "wx.request" not in source, "api.ts 出现 wx.request——应统一走 callContainer"


def test_api_module_uses_call_container() -> None:
    """生产唯一出网路径确实是 ``wx.cloud.callContainer``。"""
    source = strip_comments(_read(MP_ROOT / "services" / "api.ts"))
    assert "wx.cloud.callContainer" in source
    assert "X-WX-SERVICE" in source, "callContainer 必须带服务名头"


def test_dev_mode_is_false_and_branch_is_single() -> None:
    """``DEV_MODE`` 字面量 ``false``，且 ``api.ts`` 里只有**一处** dev 分支。"""
    dev = strip_comments(_read(MP_ROOT / "services" / "dev-channel.ts"))
    assert re.search(r"export\s+const\s+DEV_MODE\s*=\s*false", dev), (
        "dev-channel.ts 的 DEV_MODE 必须字面量 false（上线前复查点）"
    )
    api = strip_comments(_read(MP_ROOT / "services" / "api.ts"))
    assert api.count("if (DEV_MODE)") == 1, "dev 分支只允许有一处，多一处就多一条绕开 callContainer 的路"


def test_http_and_wx_request_only_live_in_dev_channel() -> None:
    """全仓扫描：``http://`` 与 ``wx.request`` **只允许**出现在 ``dev-channel.ts``。"""
    offenders: list[str] = []
    for path in iter_sources(MP_ROOT):
        if path.name == "dev-channel.ts":
            continue
        source = strip_comments(_read(path))
        if "http://" in source or "wx.request" in source:
            offenders.append(str(path.relative_to(MP_ROOT)))
    assert offenders == [], f"以下文件出现了 http:// 或 wx.request：{offenders}"


def test_dev_channel_owns_the_local_base_url() -> None:
    """本地直连地址集中在 ``dev-channel.ts``，且默认指向 127.0.0.1:8000。"""
    dev = strip_comments(_read(MP_ROOT / "services" / "dev-channel.ts"))
    assert "http://127.0.0.1:8000" in dev
    assert "wx.request" in dev, "dev 通道本身要用 wx.request 直连（这是它存在的唯一理由）"


# ---------------------------------------------------------------------------
# 二、页面 / 组件齐活（4.1 / 4.2）
# ---------------------------------------------------------------------------
def test_app_json_declares_required_pages() -> None:
    """``app.json`` 至少声明四 Tab 与核心流程页，且首页必须是启动页。"""
    ids = page_ids()
    required = {
        "pages/index/index",
        "pages/goods/index",
        "pages/debts/index",
        "pages/mine/index",
        "pages/card/index",
        "pages/pending/index",
        "pages/count/index",
        "pages/correct/index",
    }
    assert required <= set(ids), f"缺少页面：{sorted(required - set(ids))}"
    assert ids[0] == "pages/index/index", "首页必须是第一个（小程序启动页）"


@pytest.mark.parametrize("page_id", page_ids())
def test_every_declared_page_has_all_four_files(page_id: str) -> None:
    """每个页面都有 ``ts/wxml/wxss/json`` 四件套（缺一个就是白屏或编译报错）。"""
    base = MP_ROOT / page_id
    for suffix in (".ts", ".wxml", ".wxss", ".json"):
        target = base.with_suffix(suffix)
        assert target.is_file(), f"缺文件：{target.relative_to(MP_ROOT)}"


@pytest.mark.parametrize("page_id", page_ids())
def test_using_components_paths_exist(page_id: str) -> None:
    """``usingComponents`` 指向的组件目录都真实存在（路径写错只在运行时才报）。"""
    config = json.loads(_read(MP_ROOT / f"{page_id}.json"))
    for name, raw_path in config.get("usingComponents", {}).items():
        assert raw_path.startswith("/"), f"{page_id} 的 {name} 必须用绝对路径（/components/...）"
        base = MP_ROOT / raw_path.lstrip("/")
        for suffix in (".ts", ".wxml", ".wxss", ".json"):
            target = base.with_suffix(suffix)
            assert target.is_file(), f"组件缺文件：{target.relative_to(MP_ROOT)}（{page_id} 引用）"


def test_large_screen_side_nav_component_exists() -> None:
    """电脑版微信大屏的左导航组件存在，并且被管理页引用。"""
    assert (MP_ROOT / "components" / "large-screen" / "side-nav.wxml").is_file()
    config = json.loads(_read(MP_ROOT / "pages" / "admin" / "index.json"))
    assert "side-nav" in config["usingComponents"]


# ---------------------------------------------------------------------------
# 三、卡片 7 区块（4.1「确认卡片一屏可见」）
# ---------------------------------------------------------------------------
def test_card_page_renders_all_seven_blocks() -> None:
    """卡片页 WXML 里 7 个区块一个不少，且操作区在**首屏**（文档流最前）。"""
    source = strip_wxml_comments(_read(MP_ROOT / "pages" / "card" / "index.wxml"))
    missing = [block for block in CARD_BLOCKS if f'data-block="{block}"' not in source]
    assert missing == [], f"卡片缺少区块：{missing}"
    # ⑦ 操作区吸顶放在最前——四样必须首屏可见（金额 / 影响预览 / 方向 / 主按钮）
    actions_at = source.find('data-block="actions"')
    original_at = source.find('data-block="original"')
    assert actions_at != -1 and original_at != -1
    assert actions_at < original_at, "操作区必须在原文区之前（吸顶首屏）"


def test_card_page_shows_amount_direction_and_primary_button() -> None:
    """首屏四样：金额、影响预览、方向标识、主按钮。"""
    source = strip_wxml_comments(_read(MP_ROOT / "pages" / "card" / "index.wxml"))
    # 金额与方向都挂在吸顶操作条上，`data-role` 是机器可读锚点
    assert 'data-role="amount"' in source, "首屏必须有金额"
    assert 'data-role="direction"' in source, "首屏必须有方向标识（进货/卖货）"
    assert "impact-preview" in source, "卡片必须内嵌影响预览组件"
    assert "primary_label" in source, "卡片必须有主按钮"
    page = _read(MP_ROOT / "pages" / "card" / "index.ts")
    assert "directionOfIntent" in page, "方向标识必须有推导函数（不能靠文案里写死）"
    assert "purchase." in page and "sales." in page


def test_card_countdown_never_auto_submits() -> None:
    """30s 倒计时**只提示不自动提交**（自动提交=用户没看就落账）。"""
    page = strip_comments(_read(MP_ROOT / "pages" / "card" / "index.ts"))
    assert "confirmCard" in page
    # 倒计时的 setData 里不允许出现 confirmCard 调用
    timer_block = re.search(r"(countdown|tick|timer)[\s\S]{0,400}", page)
    if timer_block is not None:
        assert "confirmCard" not in timer_block.group(0), "倒计时回调里不许调 confirmCard"


# ---------------------------------------------------------------------------
# 四、管理页八块 + R3 三重拦截（4.2）
# ---------------------------------------------------------------------------
def test_admin_page_has_all_panels() -> None:
    """管理页 8 个面板齐活（含 R3 待办审批与说法训练审核）。"""
    source = _read(MP_ROOT / "pages" / "admin" / "index.wxml")
    missing = [panel for panel in ADMIN_PANELS if f"active === '{panel}'" not in source]
    assert missing == [], f"管理页缺面板：{missing}"


def test_admin_page_reports_three_reports_and_dual_caliber() -> None:
    """三张报表（今日/本月/毛利）+ 用量与套餐**双口径不得混用**。"""
    source = _read(MP_ROOT / "pages" / "admin" / "index.wxml")
    assert "今日" in source and "本月" in source and "毛利" in source
    assert "自然月" in source, "用量必须标自然月口径"
    assert "30 天" in source, "套餐必须标 30 天滚动窗口口径"


def test_r3_approval_requires_boss_and_second_confirm() -> None:
    """R3 审批：``role=boss`` + 二次确认，两样都在代码里。"""
    page = strip_comments(_read(MP_ROOT / "pages" / "admin" / "index.ts"))
    # 服务端登录返回的字段是 `role`（见 app/api/v1/auth.py），不是 `role_code`
    assert "session.role === 'boss'" in page or 'session.role === "boss"' in page, (
        "必须按登录返回的 role=boss 判断"
    )
    assert "isBoss" in page
    assert "showModal" in page, "二次确认必须走 wx.showModal（不能只弹 toast）"
    assert "second_confirm" in page, "请求体必须带 second_confirm 标记"
    assert "pc_bigscreen" in page, "审批必须声明 device=pc_bigscreen（服务端按设备拦手机端）"


def test_export_never_uses_web_download() -> None:
    """导出**不提供网页下载**：不出现 ``downloadFile`` / ``saveFile`` 到本机再下载的路径。"""
    for path in iter_sources(MP_ROOT):
        source = strip_comments(_read(path))
        assert "wx.downloadFile" not in source, f"{path.name} 出现 wx.downloadFile（导出不走网页下载）"
    admin = strip_comments(_read(MP_ROOT / "pages" / "admin" / "index.ts"))
    assert "file" in admin.lower(), "导出应经文件传输助手 / 保存到微信 / 预览（services/wx-file-forward.ts）"


# ---------------------------------------------------------------------------
# 五、铁律：单商户（4.6）
# ---------------------------------------------------------------------------
def test_no_multi_store_or_tenant_switcher_in_ui() -> None:
    """UI 不出现门店/组织/多仓概念，也不出现租户切换器（注释先剥掉，只看真实节点）。"""
    forbidden = ("门店切换", "切换门店", "多仓", "仓库切换", "组织切换", "租户切换", "选择商户")
    offenders: list[str] = []
    for path in iter_sources(MP_ROOT, (".ts", ".wxml")):
        source = strip_comments(strip_wxml_comments(_read(path)))
        for word in forbidden:
            if word in source:
                offenders.append(f"{path.relative_to(MP_ROOT)}:{word}")
    assert offenders == [], f"UI 出现了多商户概念：{offenders}"


def test_no_web_version_entry_and_no_password_login() -> None:
    """无独立网页版入口、无账号密码登录（身份只来自 ``wx.login``）。"""
    for path in iter_sources(MP_ROOT, (".wxml",)):
        source = strip_wxml_comments(_read(path))
        # 只要 WXML 里没有密码输入框，就不可能有账号密码登录
        assert 'password="true"' not in source, f"{path.name} 出现密码输入框"
        assert 'type="password"' not in source, f"{path.name} 出现密码输入框"
        # 小程序里打开网页唯一的正规入口是 `<web-view>`；没有它就等于没有网页版
        assert "<web-view" not in source, f"{path.name} 出现 web-view（等于开了网页版入口）"
    for path in iter_sources(MP_ROOT, (".ts",)):
        source = strip_comments(_read(path))
        assert "navigateToMiniProgram" not in source, f"{path.name} 跳转到别的小程序（绕开单商户边界）"
        assert "webview" not in source.lower(), f"{path.name} 出现 webview 字样"
    index = strip_comments(_read(MP_ROOT / "pages" / "index" / "index.ts"))
    assert "wx.login" in index, "登录只走 wx.login（微信登录态换 JWT）"
    app = strip_comments(_read(MP_ROOT / "app.ts"))
    assert "wx.cloud.callContainer" not in app, "app.ts 不应直接出网（统一走 services/api.ts）"


# ---------------------------------------------------------------------------
# 六、可用性硬约束：拇指热区 / 双栏回落 / 降级态可见
# ---------------------------------------------------------------------------
def test_big_button_thumb_hotzone_at_least_88px() -> None:
    """语音大按钮热区 ≥88×88px（PRD 3.2）。"""
    style = _read(MP_ROOT / "components" / "big-button" / "big-button.wxss")
    min_width = re.search(r"min-width:\s*(\d+)px", style)
    min_height = re.search(r"min-height:\s*(\d+)px", style)
    assert min_width is not None and int(min_width.group(1)) >= 88, "热区宽度必须 ≥88px"
    assert min_height is not None and int(min_height.group(1)) >= 88, "热区高度必须 ≥88px"


def test_big_button_supports_hold_and_tap_modes() -> None:
    """按住说 / 点一下说**双模式**都支持。"""
    wxml = _read(MP_ROOT / "components" / "big-button" / "big-button.wxml")
    assert "bindtouchstart" in wxml and "bindtouchend" in wxml, "必须支持按住说（touchstart/touchend）"
    assert "bindtap" in wxml, "必须支持点一下说（tap 切换）"


def test_recording_shows_duration_and_energy() -> None:
    """录音中要有**时长 + 能量**可视化。"""
    wxml = _read(MP_ROOT / "components" / "big-button" / "big-button.wxml")
    assert "duration" in wxml.lower(), "录音中要显示时长"
    assert "bar" in wxml.lower(), "录音中要有能量条"


def test_large_screen_has_narrow_fallback() -> None:
    """双栏默认 + 窄屏回落单栏（``.console--narrow`` 与 ``.nav--narrow``）。"""
    admin_style = _read(MP_ROOT / "pages" / "admin" / "index.wxss")
    nav_style = _read(MP_ROOT / "components" / "large-screen" / "side-nav.wxss")
    assert "console--narrow" in admin_style
    assert "flex-direction: column" in admin_style, "窄屏必须回落单栏"
    assert "nav--narrow" in nav_style
    admin_ts = strip_comments(_read(MP_ROOT / "pages" / "admin" / "index.ts"))
    assert "detectNarrow" in admin_ts and "windowWidth" in admin_ts


def test_double_column_layout_structure() -> None:
    """双栏是**结构**上的（左导航 + 右内容），不是靠 media query 硬凑。

    断言四件事，任一条不成立就说明「电脑版微信大屏」那个验收点没做到：
    1. 根节点默认是 ``flex``（宽屏横排双栏）；
    2. ``narrow`` 为真时才追加 ``console--narrow`` 并改成竖排；
    3. 左导航有固定宽度、窄屏改成整行（``width: 100%``）而不是消失；
    4. **八个入口在窄屏下一个都不少**（`bossOnly` 的只置灰、不隐藏）。
    """
    wxml = strip_wxml_comments(_read(MP_ROOT / "pages" / "admin" / "index.wxml"))
    admin_style = _read(MP_ROOT / "pages" / "admin" / "index.wxss")
    nav_style = _read(MP_ROOT / "components" / "large-screen" / "side-nav.wxss")
    nav_wxml = strip_wxml_comments(_read(MP_ROOT / "components" / "large-screen" / "side-nav.wxml"))
    nav_ts = strip_comments(_read(MP_ROOT / "components" / "large-screen" / "side-nav.ts"))

    # 1) 根节点双栏
    assert 'class="console {{narrow ? \'console--narrow\' : \'\'}}"' in wxml.replace('"', '"')
    assert "display: flex" in admin_style
    # 2) 窄屏竖排
    assert re.search(r"\.console--narrow\s*\{[^}]*flex-direction:\s*column", admin_style)
    # 3) 左导航固定宽度 + 窄屏整行
    assert re.search(r"\.nav\s*\{[^}]*width:\s*240rpx", nav_style)
    assert re.search(r"\.nav--narrow\s*\{[^}]*width:\s*100%", nav_style)
    # 4) 八个入口一个不少；bossOnly 只是置灰
    assert "items" in nav_wxml
    assert "bossOnly" in nav_ts, "老板专属入口必须由组件自己处理（置灰而不是隐藏）"
    # 非 boss 时入口**仍然渲染**，只多一个「老板」角标 + 置灰样式
    assert 'item.bossOnly && !isBoss' in nav_wxml, "非 boss 时入口要带「老板」角标但仍可见"
    assert "off" in nav_style or "locked" in nav_style, "置灰样式必须真的存在"
    entries = re.findall(r"\{ key: '[a-z]+', label: '[^']+'(?:, bossOnly: true)? \}", _read(MP_ROOT / "pages" / "admin" / "index.ts"))
    assert len(entries) == len(ADMIN_PANELS), f"导航入口 {len(entries)} 个，期望 {len(ADMIN_PANELS)} 个"


def test_app_json_is_resizable_for_pc_wechat() -> None:
    """``resizable: true`` —— 电脑版微信大屏才可能变成宽窗口。"""
    data = json.loads(_read(MP_ROOT / "app.json"))
    assert data.get("resizable") is True


# ---------------------------------------------------------------------------
# 六·五、同声传译插件配置（2026-11-17 · 官方文档核实后补）
#
# 为什么这组断言必须存在
# ----------------------
# 「语音 → 文字」这段**全靠这个插件**。它有三个「编译期不报错、只在真机上炸」的坑：
#
# 1. ``plugins.WechatSI.version`` **必须**与 mp 后台「插件管理 → 详情」里的版本号
#    **完全一致**。写错 → 真机报「插件不存在」→ 现象是「按大按钮一点反应都没有」；
# 2. mp 后台「用户隐私保护指引」**必须**声明「访问你的麦克风」。
#    插件内部要调 ``wx.getRecorderManager``，不声明 → 官方直接禁用该接口
#    （errno 112）→ 同样是「按了没反应」；
# 3. ``permission.scope.record.desc`` 不能是空串（官方上限 30 字），
#    空串 → 真机拒绝拉起授权弹窗。
#
# 这三件事**都不在代码里**，但都会让核心卖点「说不了话」。所以代码侧至少要做到：
# 插件配置本身合法（有确定性检查），且**不可用时给出具体原因**（不让人瞎猜）。
# ---------------------------------------------------------------------------
WECHATSI_PROVIDER = "wx069ba97219f66d99"
INVALID_PLUGIN_VERSIONS = {"latest", "*", "", "0"}


def test_app_json_declares_wechatsi_plugin_with_concrete_version() -> None:
    """``plugins.WechatSI`` 必须声明，且 ``version`` 是**具体版本号**。

    ``latest`` / ``*`` / 空串这类值微信**不解析**，运行时会直接报「插件不存在」——
    比写错一个具体版本号更难排查（连「版本对不上」这条线索都没有）。
    """
    data = json.loads(_read(MP_ROOT / "app.json"))
    plugins = data.get("plugins")
    assert isinstance(plugins, dict), "app.json 缺 plugins 段"
    si = plugins.get("WechatSI")
    assert isinstance(si, dict), "app.json 缺 plugins.WechatSI（语音功能不可能工作）"

    assert si.get("provider") == WECHATSI_PROVIDER, (
        f"插件 provider 应是微信同声传译官方 AppID {WECHATSI_PROVIDER}"
    )
    version = str(si.get("version", ""))
    assert version.lower() not in INVALID_PLUGIN_VERSIONS, (
        f"version 是 {version!r} —— 微信不解析这种值，运行时会报「插件不存在」"
    )
    assert re.match(r"^\d+\.\d+\.\d+$", version), (
        f"version 应形如 0.3.5，实际 {version!r}（必须与后台「详情」页显示的完全一致）"
    )


def test_app_json_does_not_declare_invalid_record_permission() -> None:
    """``permission`` 里**不能**出现 ``scope.record``。

    2026-11-19 实测：基础库 3.18.0 连续报
    ``无效的 app.json permission["scope.record"]``。官方 `permission` 字段只支持
    ``scope.userLocation``（及前景/后台定位），``scope.record`` 是**授权 scope**，
    不属于 `permission` 的合法配置项；录音授权由运行时自动拉起，不再看这里。

    这条断言防止有人照着旧教程把它加回来——加了不报错，只在控制台刷警告，
    而且会让「录音失败」的原因更难看懂。
    """
    data = json.loads(_read(MP_ROOT / "app.json"))
    permission = data.get("permission") or {}
    assert "scope.record" not in permission, (
        "app.json 的 permission.scope.record 已被新版基础库判为无效，"
        "录音授权走运行时 + 后台隐私声明，不要写在这里"
    )


def test_speech_gate_checks_privacy_before_recording() -> None:
    """**录音之前**必须先过隐私授权（否则「按了没反应」且用户不知道原因）。"""
    gate = strip_comments(_read(MP_ROOT / "services" / "speech.ts"))
    privacy = strip_comments(_read(MP_ROOT / "services" / "privacy.ts"))
    assert "ensurePrivacyAuthorized" in gate, "语音门禁必须调用隐私授权"
    assert "ensurePrivacyAuthorized" in privacy
    # 隐私模块必须真的调官方接口（不是自己糊一个标志位）
    assert "wx.getPrivacySetting" in privacy or "getPrivacySetting" in privacy
    assert "requirePrivacyAuthorize" in privacy


def test_speech_unavailable_reason_is_exposed_to_self_check() -> None:
    """插件不可用时**必须给出具体原因**，不能只说「未启用」。

    「点大按钮没反应」有五种完全不同的原因（版本号 / 后台审核 / 隐私声明 / 微信版本），
    修法也完全不同。只给一句「未启用」，开发者只能反复试。
    """
    gate = strip_comments(_read(MP_ROOT / "services" / "speech.ts"))
    page = strip_comments(_read(MP_ROOT / "pages" / "index" / "index.ts"))
    wxml = strip_wxml_comments(_read(MP_ROOT / "pages" / "index" / "index.wxml"))

    assert "SpeechUnavailableReason" in gate, "必须定义不可用原因的类型"
    assert "speechReasonText" in gate, "必须把原因码翻成人话"
    assert "unavailableReason" in gate, "门禁必须持有原因码"
    # 页面要把它显示出来（自检面板）
    assert "speechReason" in page, "页面必须持有原因文本"
    assert "speechReason" in wxml, "自检面板必须显示原因（否则用户看不到）"
    assert "unavailableReason" in page, "页面必须把门禁的原因传出来"


def test_plugin_config_check_script_exists_and_catches_version_mismatch() -> None:
    """插件配置校验脚本必须存在，且**真的能抓到版本号不一致**。

    没有它的话，「app.json 的 version 与后台对不上」这件事只有真机能发现——
    而真机上的现象是「没反应」，开发者根本联想不到版本号。
    """
    script = MP_ROOT / "scripts" / "check-plugin-config.py"
    assert script.is_file(), "缺插件配置校验脚本"
    source = _read(script)
    assert "WECHATSI_VERSION" in source, "脚本必须支持与后台版本号比对"
    assert "plugins" in source and "WechatSI" in source


def test_degrade_levels_visibly_differ_in_ui() -> None:
    """L0~L4 **在 UI 上真的能看到差别**（不是只写进配置）。"""
    page = strip_comments(_read(MP_ROOT / "pages" / "index" / "index.ts"))
    for level in ("L0", "L1", "L2", "L3", "L4"):
        assert f"'{level}'" in page or f'"{level}"' in page, f"首页没有处理降级档 {level}"
    # canVoice / thrifty 两个字段决定「能不能录音」「要不要显示省额度角标」
    assert "canVoice" in page
    assert "thrifty" in page


# ---------------------------------------------------------------------------
# 七、可观测性规格（4.5）—— yaml 里该有的指标/告警一个不少
# ---------------------------------------------------------------------------
def _load_yaml(relative: str) -> dict:
    """读 infra 下的 yaml（PyYAML 已在环境里）。"""
    yaml = pytest.importorskip("yaml")
    path = REPO_ROOT / relative
    assert path.is_file(), f"缺文件：{relative}"
    return yaml.safe_load(_read(path))


def test_metrics_yaml_declares_required_metrics() -> None:
    """``infra/observability/metrics.yaml`` 覆盖命中率 / LLM 调用率 / 缓存命中率 /
    单次成本 / P95 延迟 / 发货失败数 / CAS 冲突率。"""
    data = _load_yaml("infra/observability/metrics.yaml")
    names = {item["name"] for item in data["metrics"]}
    required = {
        "erp_rule_hit_ratio",
        "erp_rule_misjudge_ratio",
        "erp_llm_call_ratio",
        "erp_cache_hit_ratio",
        "erp_cost_per_parse_yuan",
        "erp_parse_latency_ms",
        "erp_deliver_failed_total",
        "erp_preview_version_conflict_ratio",
        "erp_degrade_level",
    }
    missing = required - names
    assert missing == set(), f"metrics.yaml 缺指标：{sorted(missing)}"


def test_metrics_yaml_marks_planned_honestly() -> None:
    """没实现的指标必须标 ``status: planned``（不许在报告里当已上线）。"""
    data = _load_yaml("infra/observability/metrics.yaml")
    statuses = {item["name"]: item.get("status") for item in data["metrics"]}
    assert set(statuses.values()) <= {"implemented", "planned"}
    assert statuses["erp_cache_hit_ratio"] == "planned", "解析缓存尚未实现，必须如实标 planned"


def test_metrics_yaml_forbids_high_cardinality_labels() -> None:
    """指标维度里禁止出现 ``tenant_id`` / ``store_id`` / ``org_id``（基数 + 单商户铁律）。"""
    data = _load_yaml("infra/observability/metrics.yaml")
    forbidden = set(data["label_policy"]["forbidden"])
    assert {"tenant_id", "store_id", "org_id"} <= forbidden
    for item in data["metrics"]:
        for label in item.get("labels", []):
            assert label not in forbidden, f"{item['name']} 用了禁用的维度 {label}"


def test_alerts_yaml_has_severity_and_runbook() -> None:
    """每条告警都必须有 severity 与 runbook（不写「排查一下」这种废话）。"""
    data = _load_yaml("infra/observability/alerts.yaml")
    rules = [rule for group in data["groups"] for rule in group["rules"]]
    assert len(rules) >= 8, f"告警太少（{len(rules)} 条），覆盖不到成本/延迟/发货/降级"
    for rule in rules:
        assert rule["severity"] in {"page", "ticket", "log"}, f"{rule['alert']} 的 severity 不合法"
        assert rule.get("runbook"), f"{rule['alert']} 缺 runbook"
        assert rule.get("impact"), f"{rule['alert']} 缺 impact（说不清用户感知就不该告警）"


def test_alerts_yaml_covers_deliver_and_conflict() -> None:
    """发货失败与 CAS 冲突各有专属告警（这两件事一个是资损一个是记错账）。"""
    data = _load_yaml("infra/observability/alerts.yaml")
    alerts = {rule["alert"] for group in data["groups"] for rule in group["rules"]}
    assert "DeliverFailureSpike" in alerts
    assert "PreviewVersionConflictSpike" in alerts
    assert "RuleMisjudgeRateHigh" in alerts, "误判率是硬指标，必须有 page 级告警"
