"""并发串味（cross-tenant bleed）+ 中间件形态：**硬证据，不是逻辑推演**。

为什么要有这个文件
------------------
「租户上下文按请求隔离」在多租户系统里是**不变式**，但它会被两种东西悄悄破坏：

1. 中间件把下游应用放进**另一个 task**（``BaseHTTPMiddleware`` 的经典坑）
   → 下游与中间件的 context 不是同一个，``bind()`` / ``reset()`` 可能错位；
2. ``bind()`` 与 ``reset()`` 被拆进两个 **context 副本**（同步 ``yield`` 依赖 +
   线程池的 ``contextmanager_in_threadpool``）

两者都**不会立刻报错**，只会在某次并发下让 A 商户看到 B 商户的数据——
这正是最难查、后果最重的那类缺陷。所以本文件把它变成可执行断言：

- **双租户 × 50 并发**打**真实 ASGI app**（``httpx.ASGITransport``，**不是** TestClient），
  两个租户的请求用 ``asyncio.gather`` 混跑，每个请求带自己租户的真 JWT，
  命中会**回显租户数据**的只读端点；断言每个响应里的数据**只**属于它自己的租户；
- 断言 ``context_reset_cross_context_total == 0``（不变式的读数）；
- 并发跑完**紧接着再发一次**，并直接检查测试所在 task 里没有残留上下文
  （``ASGITransport`` 就在调用方 task 里跑 app，漏了复位会当场看见）；
- **反证**计数器不是死代码（bind/reset 被拆进两个 context 时必然 +1）；
- 两个中间件的形态约束：栈里不许出现 ``BaseHTTPMiddleware``、非 http scope 必须透传、
  体积守卫**不得**一次性 buffering 整个 body、幂等中间件**不得**缓存异常结果、
  幂等键必须按租户分命名空间。

为什么用**文件版 SQLite** 而不是 ``:memory:``
---------------------------------------------
内存 SQLite 走 ``StaticPool``：全进程共用**一条**连接，多线程并发直接
``sqlite3.InterfaceError: bad parameter or other API misuse``（本机实测）。
这是夹具的物理限制，不是被测代码的问题，所以并发用例换文件版（每连接独立）。
并发写会有锁竞争，因此用例只打**只读**端点（``GET /query/stock``）——读并发才真实。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import httpx
import pytest

from app.common.idempotency import IdempotencyMiddleware, MemoryLRUStore
from app.config import Settings
from app.db.session import Database
from app.main import RequestSizeGuardMiddleware, create_app
from app.models.catalog import Product
from app.models.inventory import Inventory
from app.tenant.context import TenantContext, bind, is_bound, reset
from app.tenant.metrics import context_safety_snapshot, reset_context_safety_counters

#: 每个租户的并发请求数（团队指定的口径：双租户 × 50）
CONCURRENCY_PER_TENANT = 50


# ---------------------------------------------------------------------------
# 夹具：两个真租户（真登录 + 真 JWT + 各自专属商品）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Caller:
    """一个租户的调用方：真 JWT + **只属于它自己**的商品名（判定串味的锚点）。"""

    label: str
    tenant_id: uuid.UUID
    store_id: uuid.UUID
    token: str
    product_names: frozenset[str]

    @property
    def headers(self) -> dict[str, str]:
        """带自己租户 JWT 的请求头。"""
        return {"Authorization": f"Bearer {self.token}"}


@dataclass(frozen=True)
class Live:
    """一套跑起来的 ASGI app + 客户端 + 两个租户。"""

    app: Any
    database: Database
    client: httpx.AsyncClient
    a: Caller
    b: Caller


def _make_settings(db_path: Path) -> Settings:
    """测试配置：**文件版** SQLite（并发需要每连接独立）+ 固定 JWT 密钥 + fake 微信。"""
    return Settings(
        env="test",
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        jwt_secret="test-secret-for-tenant-isolation-do-not-reuse",
        wx_provider="fake",
    )


async def _open_tenant(
    client: httpx.AsyncClient,
    database: Database,
    *,
    code: str,
    label: str,
    names: tuple[str, ...],
) -> Caller:
    """登录开通一个租户，并给它塞两条**只有它能看见**的库存。"""
    response = await client.post("/api/v1/auth/login", json={"code": code})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    tenant_id = uuid.UUID(data["tenant_id"])
    store_id = uuid.UUID(data["store_id"])
    with database.session(bypass_scope=True) as session:
        for name in names:
            product_id = uuid.uuid4()
            session.add(
                Product(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    name=name,
                    spec="",
                    base_unit="瓶",
                    last_purchase_price_fen=100,
                    last_sale_price_fen=200,
                )
            )
            session.add(
                Inventory(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product_id=product_id,
                    qty_base=Decimal("10"),
                    version=0,
                )
            )
    return Caller(
        label=label,
        tenant_id=tenant_id,
        store_id=store_id,
        token=str(data["jwt"]),
        product_names=frozenset(names),
    )


@pytest.fixture()
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    """起一个真 ASGI app（**不经 TestClient**），两个租户各带两条专属库存。"""
    application = create_app(_make_settings(tmp_path / "isolation.db"))
    database: Database = application.state.database
    database.create_all()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi.test") as client:
        first = await _open_tenant(
            client,
            database,
            code="wx-isolation-a",
            label="A",
            names=("A专属可乐", "A专属雪碧"),
        )
        second = await _open_tenant(
            client,
            database,
            code="wx-isolation-b",
            label="B",
            names=("B专属可乐", "B专属雪碧"),
        )
        reset_context_safety_counters()
        try:
            yield Live(app=application, database=database, client=client, a=first, b=second)
        finally:
            database.dispose()


async def _hit(client: httpx.AsyncClient, caller: Caller) -> tuple[Caller, int, frozenset[str], str]:
    """打一次只读端点，回收「这个响应里出现了哪些商品名」。"""
    response = await client.get("/api/v1/query/stock", headers=caller.headers)
    if response.status_code != 200:
        return caller, response.status_code, frozenset(), response.text
    try:
        items = response.json()["data"]["items"]
    except (KeyError, TypeError):  # pragma: no cover - 契约破坏时给出可读失败
        return caller, response.status_code, frozenset(), response.text
    return caller, response.status_code, frozenset(item["product_name"] for item in items), ""


# ---------------------------------------------------------------------------
# ① 双租户 × 50 并发：一条串味都不许有
# ---------------------------------------------------------------------------
async def test_fifty_concurrent_requests_per_tenant_never_bleed(live: Live) -> None:
    """两个租户各 50 个请求混跑：每个响应只能看见**自己租户**的商品。"""
    calls: list[Any] = []
    for _ in range(CONCURRENCY_PER_TENANT):
        # 交错入队：让 A/B 的请求在事件循环里真正交替，而不是「A 全跑完再 B」
        calls.append(_hit(live.client, live.a))
        calls.append(_hit(live.client, live.b))

    results = await asyncio.gather(*calls)

    assert len(results) == 2 * CONCURRENCY_PER_TENANT
    offenders: list[str] = []
    served = {live.a.label: 0, live.b.label: 0}
    for caller, status, names, text in results:
        if status != 200:
            offenders.append(f"[{caller.label}] 状态 {status}：{text[:200]}")
            continue
        served[caller.label] += 1
        if names != caller.product_names:
            offenders.append(
                f"[{caller.label}] 期望 {sorted(caller.product_names)}，实际 {sorted(names)}"
            )

    assert offenders == [], f"{len(offenders)} 条串味/失败，前 3 条：{offenders[:3]}"
    assert served == {
        live.a.label: CONCURRENCY_PER_TENANT,
        live.b.label: CONCURRENCY_PER_TENANT,
    }

    snapshot = context_safety_snapshot()
    assert snapshot.context_reset_cross_context_total == 0, (
        "并发期间出现跨 context 复位：bind/reset 被拆进了两个 context 副本，"
        "租户上下文错位的风险已经出现"
    )
    assert snapshot.healthy is True
    # 打过 -s 就能在终端直接看到这次并发的读数（不是靠推理得出的结论）
    print(
        f"\n[并发串味] 请求={2 * CONCURRENCY_PER_TENANT}"
        f"（{live.a.label} {CONCURRENCY_PER_TENANT} / {live.b.label} {CONCURRENCY_PER_TENANT}）"
        f" 响应 200={served[live.a.label] + served[live.b.label]}"
        f" 串味={len(offenders)}"
        f" context_reset_cross_context_total={snapshot.context_reset_cross_context_total}"
    )


# ---------------------------------------------------------------------------
# ② 并发跑完之后：没有残留
# ---------------------------------------------------------------------------
async def test_no_context_residue_after_the_concurrent_burst(live: Live) -> None:
    """回头再发一次：上下文没残留、数据还是自己的、计数器仍然为 0。"""
    await asyncio.gather(*[_hit(live.client, caller) for caller in (live.a, live.b)])

    # ASGITransport 就在**本测试所在的 task** 里调用 app：漏了复位会当场看见
    assert is_bound() is False, "并发之后当前 task 还留着租户上下文：中间件没复位"

    for caller in (live.a, live.b):
        owner, status, names, text = await _hit(live.client, caller)
        assert status == 200, text
        assert names == owner.product_names
        assert is_bound() is False, f"[{caller.label}] 请求结束后上下文仍然绑定着"

    assert context_safety_snapshot().context_reset_cross_context_total == 0


# ---------------------------------------------------------------------------
# ③ 反证：计数器不是死代码
# ---------------------------------------------------------------------------
async def test_counter_actually_fires_when_bind_and_reset_are_split() -> None:
    """把 bind/reset 拆进两个 context 副本，计数器**必须** +1。

    否则「计数器 == 0」可能只是因为它从来没被触发过（死代码），
    那样 ①② 的断言就成了心理安慰。
    """
    from starlette.concurrency import run_in_threadpool

    context = TenantContext(
        tenant_id=uuid.uuid4(),
        store_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
        role_code="boss",
        tenant_version=1,
    )
    holder: dict[str, Any] = {}

    def _enter() -> None:
        """在第一个线程的 context 副本里 bind（对应线程池里的依赖进入）。"""
        holder["token"] = bind(context)

    def _exit() -> None:
        """在第二个线程的 context 副本里 reset（对应线程池里的依赖退出）。"""
        reset(holder["token"], request_id="unit-test-split")

    reset_context_safety_counters()
    try:
        await run_in_threadpool(_enter)
        await run_in_threadpool(_exit)

        snapshot = context_safety_snapshot()
        assert snapshot.context_reset_cross_context_total == 1
        assert snapshot.healthy is False
    finally:
        reset_context_safety_counters()
        assert is_bound() is False


# ---------------------------------------------------------------------------
# ④ 中间件形态：不许再把下游放进另一个 task
# ---------------------------------------------------------------------------
def _middleware_chain(app: Any) -> list[Any]:
    """把 Starlette 的中间件嵌套链拍平（外层 -> 内层）。"""
    from starlette.middleware.base import BaseHTTPMiddleware

    stack = app.middleware_stack if app.middleware_stack is not None else app.build_middleware_stack()
    chain: list[Any] = []
    node: Any = stack
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        if isinstance(node, BaseHTTPMiddleware):
            break
        node = getattr(node, "app", None)
    return chain


async def test_no_middleware_wraps_downstream_into_another_task(live: Live) -> None:
    """栈里不许出现 ``BaseHTTPMiddleware``，两个守卫必须**真的**装上。"""
    from starlette.middleware.base import BaseHTTPMiddleware

    declared = [middleware.cls for middleware in live.app.user_middleware]
    assert not any(issubclass(cls, BaseHTTPMiddleware) for cls in declared), (
        f"声明了 BaseHTTPMiddleware：{[cls.__name__ for cls in declared]}"
    )
    assert RequestSizeGuardMiddleware in declared, "请求体守卫没装"
    assert IdempotencyMiddleware in declared, "幂等中间件没装"

    chain = _middleware_chain(live.app)
    offenders = [type(node).__name__ for node in chain if isinstance(node, BaseHTTPMiddleware)]
    assert offenders == [], f"实际运行的栈里有跨 task 形态：{offenders}"
    assert any(isinstance(node, RequestSizeGuardMiddleware) for node in chain)
    assert any(isinstance(node, IdempotencyMiddleware) for node in chain)


async def test_middlewares_pass_through_non_http_scope() -> None:
    """``lifespan`` / ``websocket`` 必须原样透传（否则启动就会挂）。"""
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> Any:
        seen.append(str(scope["type"]))
        return None

    guard = RequestSizeGuardMiddleware(app, max_bytes=8)
    idempotent = IdempotencyMiddleware(app, store=MemoryLRUStore(), settings=None)
    for middleware in (guard, idempotent):
        await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "lifespan"]


# ---------------------------------------------------------------------------
# ⑤ 体积守卫：边读边数、越界立刻停（不是先收完再判）
# ---------------------------------------------------------------------------
async def test_size_guard_stops_reading_once_the_limit_is_crossed() -> None:
    """超过上限**立刻**中断，剩下的字节一个都不读（100KiB 断言不能退化成整包缓冲）。"""
    limit = 8
    chunks = [b"1234"] * 8  # 32 字节，上限 8 → 第 3 块（累计 12）越界
    consumed = {"chunks": 0, "bytes": 0}
    cursor = {"index": 0}
    sent: list[Mapping[str, Any]] = []

    async def receive() -> Mapping[str, Any]:
        index = cursor["index"]
        cursor["index"] += 1
        if index >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        body = chunks[index]
        consumed["chunks"] += 1
        consumed["bytes"] += len(body)
        return {"type": "http.request", "body": body, "more_body": index < len(chunks) - 1}

    async def downstream(scope: Any, rcv: Any, snd: Any) -> None:
        """把 body 读到 EOF 的正常下游（会一直调 receive 直到 more_body=False）。"""
        while True:
            message = await rcv()
            if not message.get("more_body", False):
                break
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok"})

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    middleware = RequestSizeGuardMiddleware(downstream, max_bytes=limit)
    await middleware({"type": "http", "method": "POST", "path": "/x", "headers": []}, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert consumed["chunks"] == 3, "越界那一次之后不许再读"
    assert cursor["index"] == 3, f"剩余 {8 - 3} 块不该被读（实际读了 {cursor['index']} 次）"
    payload = json.loads(sent[1]["body"])
    assert payload["data"]["error_code"] == "E_AUDIO_TOO_LARGE"

    # 只读一次就直接判定越界：读取次数必须等于「越界所在块」，不是总块数
    assert consumed["bytes"] == 12
    assert cursor["index"] < len(chunks)


async def test_size_guard_rejects_declared_length_before_touching_downstream() -> None:
    """``content-length`` 已声明超限 → 连下游都不进。"""
    reached: list[bool] = []
    sent: list[Mapping[str, Any]] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-length", b"102401")]}
    middleware = RequestSizeGuardMiddleware(downstream, max_bytes=100 * 1024)
    await middleware(scope, _noop_receive, send)

    assert reached == [], "声明已超限却仍然进了下游"
    assert sent[0]["status"] == 413


# ---------------------------------------------------------------------------
# ⑥ 幂等：异常不缓存 + 键按租户分命名空间
# ---------------------------------------------------------------------------
async def _noop_receive() -> Mapping[str, Any]:
    """空的 receive（GET/POST 无体）。"""
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(message: Mapping[str, Any]) -> None:
    """空的 send。"""
    return None


async def test_idempotency_never_caches_a_failed_request() -> None:
    """下游抛异常 → 释放占位、**不写入「成功」结果**，下次还能真执行。"""
    attempts = {"count": 0}

    async def boom(scope: Any, receive: Any, send: Any) -> None:
        attempts["count"] += 1
        raise RuntimeError("下游炸了")

    store = MemoryLRUStore()
    middleware = IdempotencyMiddleware(boom, store=store, settings=None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(b"idempotency-key", b"card-1")],
    }

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await middleware(scope, _noop_receive, _noop_send)

    assert attempts["count"] == 2, "占位没被释放：一次崩溃被当成了「首次执行的正确结果」"
    assert await store.get("anon:card-1") is None


async def test_idempotency_key_is_namespaced_per_tenant(live: Live) -> None:
    """同一幂等键在两个租户下必须是**两条**记录（否则 A 的导出结果会重放给 B）。"""
    keys: list[str] = []

    class _SpyStore(MemoryLRUStore):
        """记录被用到的键（只读观察，不改状态机）。"""

        async def begin(self, key: str) -> str:
            keys.append(key)
            return await super().begin(key)

    async def ok_app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"code":0}', "more_body": False})

    spy = _SpyStore()
    middleware = IdempotencyMiddleware(ok_app, store=spy, settings=live.app.state.settings)

    for caller in (live.a, live.b):
        headers = [
            (b"idempotency-key", b"export:stock:csv"),
            (b"authorization", caller.headers["Authorization"].encode("latin-1")),
        ]
        scope = {"type": "http", "method": "POST", "path": "/exports", "headers": headers}
        await middleware(scope, _noop_receive, _noop_send)

    assert len(keys) == 2
    assert keys[0] != keys[1], "同一幂等键在两个租户下撞成了一条记录"
    assert keys[0].startswith(f"{live.a.tenant_id}:"), keys[0]
    assert keys[1].startswith(f"{live.b.tenant_id}:"), keys[1]
