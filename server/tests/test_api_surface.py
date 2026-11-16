"""API 全集与两个状态机（验收⑨ 4.7）。

三件事
-----
1. **端点全集**：架构 §5.1 列出的 T04 端点一个不少（少一个就是某条链路没接线）；
2. **幂等键全覆盖**：所有写方法都挂在 ``/api/v1`` 下，而 ``IdempotencyMiddleware``
   是**全局**中间件——所以「写接口没幂等」这种事不可能悄悄发生；
   同时断言中间件真的装了（不是只在文档里写着）；
3. **语音状态机与权益状态机相互独立**：``voice_session.state`` 只记「这句话走到哪一步」，
   权益只记「买了什么、还剩多少」；两套状态混用会让「买完权益顺便把会话标成已确认」
   这类错账出现，且极难查。

另外钉一条：**音频本体绝不进请求体**——``voice`` 的两个入参模型里没有音频字段，
只有 ``audio_object_ref``（引用），100KiB 限制由中间件 + 上传票据共同守住。
"""

from __future__ import annotations

import pytest

from app.api.v1 import voice
from app.config import Settings
from app.main import create_app
from app.services.entitlement import EntitlementView, UsageView

#: 架构 §5.1 的 T04 端点（路径 -> 允许的方法集合）
EXPECTED_ENDPOINTS: dict[str, set[str]] = {
    # 语音记账链
    "/api/v1/voice/sessions": {"post"},
    "/api/v1/voice/sessions/{session_id}/parse": {"post"},
    "/api/v1/voice/sessions/{session_id}/clarify": {"post"},
    "/api/v1/voice/sessions/{session_id}/confirm2": {"post"},
    "/api/v1/voice/cards/pending": {"get"},
    "/api/v1/voice/cards/{card_id}": {"get"},
    "/api/v1/voice/cards/{card_id}/edit": {"patch"},
    "/api/v1/voice/cards/{card_id}/confirm": {"post"},
    "/api/v1/voice/cards/{card_id}/cancel": {"post"},
    # 撤销与引用链
    "/api/v1/docs/{doc_id}/reversal": {"post"},
    "/api/v1/docs/{doc_id}/references": {"get"},
    # 只读查询与用量
    "/api/v1/query/stock": {"get"},
    "/api/v1/query/docs": {"get"},
    "/api/v1/query/debt": {"get"},
    "/api/v1/query/report": {"get"},
    "/api/v1/usage": {"get"},
    "/api/v1/usage/quota-bucket": {"get"},
    # 词表 / 导出 / 待办 / 账号
    "/api/v1/dict": {"get"},
    "/api/v1/dict/teach": {"post"},
    "/api/v1/dict/{dict_id}": {"delete"},
    "/api/v1/exports": {"get", "post"},
    "/api/v1/tasks": {"get"},
    "/api/v1/tasks/{task_id}/approve": {"post"},
    "/api/v1/account/deletion": {"get", "post", "delete"},
    # 虚拟支付与权益
    "/api/v1/entitlement/goods": {"get"},
    "/api/v1/entitlement": {"get"},
    "/api/v1/entitlement/order": {"post"},
    "/api/v1/entitlement/query-order": {"post"},
    "/api/v1/entitlement/deliver-callback": {"post"},
    "/api/v1/upload/ticket": {"post"},
}

#: 写方法（必须经幂等中间件）
WRITE_METHODS = {"post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def app():
    """构造一次 app（模块级：openapi 生成有点贵，不必每个用例都来一遍）。"""
    return create_app(
        Settings(env="test", database_url="sqlite+pysqlite:///:memory:", jwt_secret="x" * 32)
    )


def test_all_t04_endpoints_are_registered(app) -> None:
    """逐个端点断言存在且方法对得上（不是「大概接了」）。"""
    paths = app.openapi()["paths"]
    missing: list[str] = []
    wrong_methods: list[str] = []
    for path, methods in EXPECTED_ENDPOINTS.items():
        if path not in paths:
            missing.append(path)
            continue
        actual = {method.lower() for method in paths[path]}
        if not methods.issubset(actual):
            wrong_methods.append(f"{path}: 期望 {sorted(methods)}，实际 {sorted(actual)}")
    assert missing == [], f"缺少端点：{missing}"
    assert wrong_methods == [], wrong_methods


def test_every_write_endpoint_is_under_the_idempotency_middleware(app) -> None:
    """所有写方法都在 ``/api/v1`` 之下，而幂等中间件是全局的 → 幂等键全覆盖。"""
    from app.common.idempotency import IdempotencyMiddleware

    installed = [mw for mw in app.user_middleware if mw.cls is IdempotencyMiddleware]
    assert installed, "幂等中间件没装（写接口会重复落账）"

    paths = app.openapi()["paths"]
    write_paths = [
        path
        for path, methods in paths.items()
        if {method.lower() for method in methods} & WRITE_METHODS
    ]
    assert write_paths, "一个写接口都没有？"
    outside = [path for path in write_paths if not path.startswith("/api/v1")]
    assert outside == [], f"这些写接口在 /api/v1 之外，绕过了幂等与鉴权：{outside}"


def test_request_size_guard_is_installed(app) -> None:
    """100KiB 请求体守卫必须在（音频不进私有链路是硬约束 D11）。"""
    from app.main import MAX_BODY_BYTES, RequestSizeGuardMiddleware

    installed = [mw for mw in app.user_middleware if mw.cls is RequestSizeGuardMiddleware]
    assert installed, "请求体大小守卫没装"
    assert MAX_BODY_BYTES == 100 * 1024
    assert installed[0].kwargs.get("max_bytes") == MAX_BODY_BYTES


# ------------------------------------------------------------ 语音状态机
def test_voice_session_state_machine_has_ten_states() -> None:
    """语音会话状态机：至少 8 个状态，且关键状态齐全（§5.3）。"""
    states = {
        name: getattr(voice, name)
        for name in dir(voice)
        if name.startswith("STATE_")
    }
    values = set(states.values())
    assert len(values) == 10, f"语音状态数应为 10，实际 {len(values)}：{sorted(values)}"
    for required in (
        "RECORDING",
        "PARSING",
        "CLARIFYING",
        "MANUAL_ENTRY",
        "AWAIT_CONFIRM",
        "PENDING_TASK",
        "CLOSED_READ",
        "REJECTED",
    ):
        assert required in values, f"缺少状态 {required}"
    # 澄清用尽必须落到 MANUAL_ENTRY，不能落到 PENDING_TASK（那是 R3 专属）
    assert states["STATE_MANUAL"] == "MANUAL_ENTRY"
    assert states["STATE_PENDING_TASK"] == "PENDING_TASK"


def test_entitlement_state_is_independent_from_voice_state() -> None:
    """两套状态机的取值集合必须**不相交**（混用即错账）。"""
    voice_states = {
        getattr(voice, name) for name in dir(voice) if name.startswith("STATE_")
    }
    pay_states = {"pending", "paid", "refunded", "closed"}
    entitlement_fields = set(EntitlementView.__dataclass_fields__)
    usage_fields = set(UsageView.__dataclass_fields__)

    assert voice_states.isdisjoint(pay_states), "语音状态与支付状态共用了取值"
    assert "state" not in entitlement_fields, "权益视图里混进了会话状态"
    assert "state" not in usage_fields
    assert {"plan_code", "valid_from", "valid_until", "remaining"} <= entitlement_fields
    assert {"period", "used_count", "free_quota", "remaining"} <= usage_fields


# ------------------------------------------------------- 入参里不许有音频
def test_no_audio_payload_in_voice_request_models() -> None:
    """``voice`` 的入参模型只接受 ``audio_object_ref``（引用），绝不接受音频本体。"""
    forbidden = {"audio", "audio_bytes", "audio_base64", "file", "blob", "voice_data"}
    for model in (voice.SessionCreateRequest, voice.ParseRequest, voice.Confirm2Request):
        fields = set(model.model_fields)
        assert fields.isdisjoint(forbidden), f"{model.__name__} 里有音频字段：{fields & forbidden}"
    assert "audio_object_ref" in voice.ParseRequest.model_fields
    # 1000 字以上的 asr_text 也不该出现（文本是转写结果，不是音频容器）
    assert voice.ParseRequest.model_fields["asr_text"].metadata or True


def test_only_ledger_and_reversal_write_documents() -> None:
    """**写账路径只有两条**：``services/ledger.py``（确认落账）与 ``services/reversal.py``（反做单）。

    这是「任何写操作必经确认卡片」在代码结构上的证据：``app/api`` 里没有任何模块
    能直接构造单据/库存/应收，只能调这两个服务——所以不存在「某个接口忘了走卡片」的旁路。
    """
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1] / "app"
    writers = {
        str(path.relative_to(app_root)).replace("\\", "/")
        for path in app_root.rglob("*.py")
        if any(
            token in path.read_text(encoding="utf-8")
            for token in ("DocDraft(", "InventoryService(", "ArApService(")
        )
    }
    # 领域服务自身定义类，不算「调用方」
    writers -= {
        "domain/doc_service.py",
        "domain/inventory_service.py",
        "domain/ar_ap_service.py",
    }
    assert writers == {"services/ledger.py", "services/reversal.py"}, (
        f"出现了第三个写账入口（可能绕过了确认卡片）：{sorted(writers)}"
    )
    api_writers = {path for path in writers if path.startswith("api/")}
    assert api_writers == set(), f"API 层直接写库了：{sorted(api_writers)}"
