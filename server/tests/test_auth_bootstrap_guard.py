"""静态护栏：``set_auth_bootstrap`` 的调用点**只允许**在 ``app/services/auth.py``。

为什么需要这条护栏
------------------
``member_login_lookup`` 策略 + ``app.auth_bootstrap`` GUC 是 RLS 的**鸡生蛋解法**：
登录时还不知道租户是谁，只能先放开「按 openid 查会员」这一条只读路径。
它一旦被别处调用，等于在业务代码里随手关掉租户隔离 —— 这类漏洞不会报错、
不会失败测试，只会**安静地泄漏数据**。

因此这里不用正则，而是用 ``ast`` 精确判断「谁真的调用了它」：

- **允许**：
  1. ``app/tenant/rls.py`` 里的**定义**（``def set_auth_bootstrap``）；
  2. 所有测试文件（它们要验证护栏本身，必须能调用）。
- **只允许调用点**：``app/services/auth.py``（登录链路唯一入口）。

同时做两条「非空洞」断言：``auth.py`` 里确实有调用（≥1），
且 ``rls.py`` 里确实有定义 —— 否则文件改名后这条护栏会变成永远通过的空壳。
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


def collect_call_sites(files: list[Path]) -> dict[str, int]:
    """扫描所有文件里的 ``set_auth_bootstrap(...)`` 调用点。

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
            if isinstance(node, ast.Call) and _call_name(node) == TARGET:
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
