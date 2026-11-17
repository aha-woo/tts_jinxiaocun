"""静态护栏：两个 RLS「鸡生蛋」引导开关的调用点被**点名**限制。

``set_auth_bootstrap``（登录）与 ``set_deliver_bootstrap``（发货）
-----------------------------------------------------------------
两者都是 RLS 的鸡生蛋解法：在还不知道租户是谁的时候，**只读**地放开一条精确反查路径
（``member_login_lookup`` / ``xpay_deliver_lookup``）。它们一旦被别处调用，
等于在业务代码里随手关掉租户隔离 —— 这类漏洞不会报错、不会失败测试，
只会**安静地泄漏数据**。

因此这里不用正则，而是用 ``ast`` 精确判断「谁真的调用了它」：

- **允许**：
  1. ``app/tenant/rls.py`` 里的**定义**；
  2. 所有测试文件（它们要验证护栏本身，必须能调用）。
- **只允许调用点**：
  - ``set_auth_bootstrap`` → ``app/services/auth.py``（登录链路唯一入口）；
  - ``set_deliver_bootstrap`` → ``app/api/v1/entitlement.py``（发货推送唯一入口）。

同时做「非空洞」断言：被允许的文件里确实有调用（≥1），``rls.py`` 里确实有定义
—— 否则文件改名后这条护栏会变成永远通过的空壳。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: 仓库内 server 目录（本文件位于 server/tests/）
SERVER_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SERVER_ROOT / "app"

#: 目标符号
TARGET = "set_auth_bootstrap"

#: 唯一允许出现**调用**的文件（相对 server/ 的 POSIX 路径）
ALLOWED_CALL_FILES = frozenset({"app/services/auth.py"})

#: 豁免：定义处（不是调用）
DEFINITION_FILES = frozenset({"app/tenant/rls.py"})

#: 发货引导开关（第二处鸡生蛋）：调用点只允许在发货回调端点
DELIVER_TARGET = "set_deliver_bootstrap"
ALLOWED_DELIVER_CALL_FILES = frozenset({"app/api/v1/entitlement.py"})


def _iter_python_files(root: Path) -> list[Path]:
    """递归列出 ``.py``（跳过缓存目录）。"""
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def _rel(path: Path) -> str:
    """转成相对 ``server/`` 的 POSIX 路径（断言输出可读）。"""
    return path.relative_to(SERVER_ROOT).as_posix()


def _call_name(node: ast.Call) -> str | None:
    """取调用目标名（支持 ``f()`` 与 ``mod.f()`` 两种写法）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def collect_call_sites(files: list[Path], *, target: str = TARGET) -> dict[str, int]:
    """扫描所有文件里的 ``target(...)`` 调用点。

    Args:
        files: 待扫描文件。
        target: 函数名（默认 :data:`TARGET`）。

    Returns:
        ``{相对路径: 调用次数}``；只包含出现过调用的文件。
    """
    sites: dict[str, int] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - 语法错误应被别的测试抓住
            raise AssertionError(f"{_rel(path)} 无法解析：{exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == target:
                key = _rel(path)
                sites[key] = sites.get(key, 0) + 1
    return sites


def has_function_def(path: Path, name: str) -> bool:
    """文件里是否有顶层/嵌套 ``def name`` 定义。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


def test_scan_covers_the_expected_codebase() -> None:
    """扫描面必须覆盖到真实代码（防止「扫了个空目录就通过」）。"""
    files = _iter_python_files(APP_ROOT)
    assert len(files) >= 40, f"只扫到 {len(files)} 个文件，扫描面可能坏了"
    rels = {_rel(path) for path in files}
    # 关键目录都必须被覆盖到（T03 新增的三个目录尤其重要）
    for expected in (
        "app/services/auth.py",
        "app/tenant/rls.py",
        "app/voice/pipeline.py",
        "app/voice/template_engine.py",
        "app/services/quota.py",
        "app/domain/intent_registry.py",
    ):
        assert expected in rels, f"扫描面缺少 {expected}"


def test_definition_still_exists_in_rls_module() -> None:
    """护栏非空洞：``rls.py`` 里必须仍有这个函数的定义。"""
    rls_path = APP_ROOT / "tenant" / "rls.py"
    assert rls_path.is_file()
    assert has_function_def(rls_path, TARGET), f"{_rel(rls_path)} 里找不到 def {TARGET}"


def test_auth_service_actually_calls_it() -> None:
    """登录链路必须真的调用它（否则这条护栏只是在防一个不存在的调用）。"""
    auth_path = APP_ROOT / "services" / "auth.py"
    sites = collect_call_sites([auth_path])
    assert sites.get("app/services/auth.py", 0) >= 1, "auth.py 应至少有 1 处调用"


def test_bootstrap_is_only_called_from_auth_service() -> None:
    """**核心断言**：``app/`` 下除 ``app/services/auth.py`` 外没有别的调用点。"""
    sites = collect_call_sites(_iter_python_files(APP_ROOT))
    offenders = {
        path: count
        for path, count in sites.items()
        if path not in ALLOWED_CALL_FILES and path not in DEFINITION_FILES
    }
    assert offenders == {}, (
        "发现越权调用 set_auth_bootstrap（等于随手关掉租户隔离）：\n"
        + "\n".join(f"  {path}: {count} 处" for path, count in sorted(offenders.items()))
        + "\n允许的调用点：" + ", ".join(sorted(ALLOWED_CALL_FILES))
    )


def test_tests_are_exempt_but_business_code_is_not() -> None:
    """测试文件豁免；但豁免**不会**让业务代码漏网（同一套扫描逻辑）。"""
    app_sites = collect_call_sites(_iter_python_files(APP_ROOT))
    everything = collect_call_sites(_iter_python_files(SERVER_ROOT))
    # 全仓扫描 ⊇ app 内扫描
    assert set(app_sites) <= set(everything)
    # 业务代码里只有白名单那一处
    assert set(app_sites) <= ALLOWED_CALL_FILES | DEFINITION_FILES


def test_member_login_lookup_policy_is_still_registered() -> None:
    """``member_login_lookup`` 策略仍在（放开范围没被偷偷扩大/删除）。"""
    rls_source = (APP_ROOT / "tenant" / "rls.py").read_text(encoding="utf-8")
    assert "member_login_lookup" in rls_source
    assert "app.auth_bootstrap" in rls_source


# --------------------------------------------------- 发货引导（第二处鸡生蛋）


def test_deliver_bootstrap_definition_still_exists() -> None:
    """护栏非空洞：``rls.py`` 里必须仍有 ``set_deliver_bootstrap`` 定义。"""
    rls_path = APP_ROOT / "tenant" / "rls.py"
    assert has_function_def(rls_path, DELIVER_TARGET), f"{_rel(rls_path)} 里找不到该定义"


def test_deliver_bootstrap_is_called_by_the_callback_endpoint() -> None:
    """发货回调确实用它（开 / 关各一次：进引导 → 拿租户 → 立刻切回租户模式）。"""
    sites = collect_call_sites(
        [APP_ROOT / "api" / "v1" / "entitlement.py"], target=DELIVER_TARGET
    )
    assert sites.get("app/api/v1/entitlement.py", 0) >= 2, (
        "发货引导开关必须既开又关（只开不关 = 事务里一直带着引导开关）"
    )


def test_deliver_bootstrap_is_only_called_from_the_callback_endpoint() -> None:
    """**核心断言**：``app/`` 下除发货回调端点外没有别的 ``set_deliver_bootstrap`` 调用点。"""
    sites = collect_call_sites(_iter_python_files(APP_ROOT), target=DELIVER_TARGET)
    offenders = {
        path: count
        for path, count in sites.items()
        if path not in ALLOWED_DELIVER_CALL_FILES and path not in DEFINITION_FILES
    }
    assert offenders == {}, (
        "发现越权调用 set_deliver_bootstrap（等于随手放开订单表读取）：\n"
        + "\n".join(f"  {path}: {count} 处" for path, count in sorted(offenders.items()))
        + "\n允许的调用点：" + ", ".join(sorted(ALLOWED_DELIVER_CALL_FILES))
    )


def test_xpay_deliver_lookup_policy_is_still_registered() -> None:
    """``xpay_deliver_lookup`` 策略仍在，且用的是**自己的**开关（不与登录共用）。"""
    rls_source = (APP_ROOT / "tenant" / "rls.py").read_text(encoding="utf-8")
    assert "xpay_deliver_lookup" in rls_source
    assert "app.deliver_bootstrap" in rls_source

    policy_block = rls_source.split("def xpay_deliver_lookup_policy_sql")[-1]
    assert "app.deliver_bootstrap" in policy_block
    assert "app.auth_bootstrap" not in policy_block, (
        "发货策略不得复用登录引导开关：共用一个开关等于登录时也能读别人的订单"
    )
