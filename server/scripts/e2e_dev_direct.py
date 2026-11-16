"""端到端 ⑦：**dev 直连**跑通「进了两箱可乐，一件三十五」全链路并打耗时分解。

为什么单独写成脚本而不是 pytest
-------------------------------
验收要的是「**dev 直连**」（``http://127.0.0.1:8000``）跑出来的真实数字，
包括 uvicorn 起服务、真实 HTTP、真实 SQLite 落盘。pytest 里跑 ``TestClient``
是进程内调用，测不出「网络 + 序列化 + 真实落盘」的那部分时间，也就没法
跟端侧 ``services/dev-channel.ts`` 的直连路径对齐。

跑法（**必须先 cd server**，pyproject 在 server/ 下）::

    cd server
    python scripts/e2e_dev_direct.py

输出
----
1. 断言：库存 8 箱 → 10 箱、金额 ¥70.00、``C_llm = 0``、端到端 ≤ 8000ms；
2. 耗时分解表：G0-G2 / ASR / G3 / G4 / 预演 / 落账 各多少 ms；
3. 每条耗时都注明是「实测」还是「= 0（本链路不经过）」，**不把 0 当成绩**。

ASR 这一段本地没有真实转写服务（需要真实 AppID + 云托管 + ASR 三方），
所以脚本用 ``asr_text`` 直接喂文本，并把 ASR 一节明确标成
``skipped(no_asr_service)``——**不许把 skipped 写成 0ms 的喜报**。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

HOST = "127.0.0.1"
PORT = int(os.getenv("E2E_PORT", "8000"))
BASE_URL = f"http://{HOST}:{PORT}"

#: 验收硬约束：端到端 ≤ 8s
MAX_TOTAL_MS = 8_000

#: 验收话术与期望值
UTTERANCE = "进了两箱可乐，一件三十五"
EXPECT_QTY_BOX = Decimal("10")  # 8 箱 + 2 箱
EXPECT_AMOUNT_FEN = 7_000  # 2 箱 × 35 元/件 = 70 元


@dataclass
class Stage:
    """一段耗时（实测或说明为何为 0 / 未测）。"""

    name: str
    ms: float
    kind: str = "measured"
    note: str = ""


@dataclass
class Report:
    """最终报告（打印用）。"""

    stages: list[Stage] = field(default_factory=list)
    assertions: list[tuple[str, bool, str]] = field(default_factory=list)

    def stage(self, name: str, ms: float, kind: str = "measured", note: str = "") -> None:
        """记一段耗时。"""
        self.stages.append(Stage(name=name, ms=ms, kind=kind, note=note))

    def check(self, label: str, passed: bool, detail: str = "") -> None:
        """记一条断言。"""
        self.assertions.append((label, passed, detail))

    @property
    def ok(self) -> bool:
        """全部断言是否通过。"""
        return all(passed for _label, passed, _detail in self.assertions)

    def render(self) -> str:
        """渲染成人看的报告。"""
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("端到端 ⑦ 耗时分解（dev 直连 http://127.0.0.1:8000）")
        lines.append("=" * 78)
        lines.append(f"{'阶段':<24}{'耗时(ms)':>12}   {'来源':<22}说明")
        lines.append("-" * 78)
        measured_total = 0.0
        for stage in self.stages:
            if stage.kind == "measured":
                measured_total += stage.ms
            lines.append(
                f"{stage.name:<24}{stage.ms:>12.1f}   {stage.kind:<22}{stage.note}"
            )
        lines.append("-" * 78)
        lines.append(f"{'实测合计':<24}{measured_total:>12.1f}")
        lines.append("")
        lines.append("断言")
        lines.append("-" * 78)
        for label, passed, detail in self.assertions:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"[{mark}] {label}" + (f" —— {detail}" if detail else ""))
        lines.append("=" * 78)
        lines.append("总体：" + ("全部通过" if self.ok else "**有失败项**"))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 最小 HTTP 客户端（只用标准库，避免给 CI 加依赖）
# ---------------------------------------------------------------------------
def call_api(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = 20.0,
) -> tuple[int, dict[str, Any]]:
    """发一次请求并解包 ``{code,data,message}``。

    Returns:
        ``(http_status, data)``；``code != 0`` 时把 ``data`` 原样返回并附带 ``_code``。
    """
    url = f"{BASE_URL}{path}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method.upper())
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:  # 4xx/5xx 也带包络，照样解
        raw = error.read().decode("utf-8")
        status = error.code
    envelope: dict[str, Any] = {}
    if raw:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            # 非 JSON（例如 500 的纯文本栈）也要能被看见，不许静默吞掉
            return status, {"_raw": raw[:400]}
    data = envelope.get("data") or {}
    if envelope.get("code", 0) != 0:
        data = {**data, "_code": envelope.get("code"), "_message": envelope.get("message")}
    return status, data


def wait_until_up(deadline_seconds: float = 30.0) -> float:
    """等端口可连（返回等待耗时 ms）。"""
    started = time.perf_counter()
    deadline = started + deadline_seconds
    while time.perf_counter() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex((HOST, PORT)) == 0:
                return (time.perf_counter() - started) * 1000
        time.sleep(0.05)
    raise RuntimeError(f"服务在 {deadline_seconds:.0f}s 内没起来：{BASE_URL}")


def port_in_use() -> bool:
    """端口是否已被占用（本地可能已有 dev server 在跑）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((HOST, PORT)) == 0


def prepare_database(database_url: str) -> None:
    """建表（34 张）。

    生产走 alembic；这里为了「一条命令跑通」直接用 metadata ``create_all``，
    与 pytest 里的内存库做法一致——**只用于本地联调，不进生产路径**。
    """
    import app.models  # noqa: F401  确保 34 张表全部注册到 metadata
    from app.db.session import Database

    database = Database.from_url(database_url)
    database.create_all()
    database.dispose()


# ---------------------------------------------------------------------------
# 种子：登录（自动开租户）→ 补商品 / 库存 / 换算
# ---------------------------------------------------------------------------
def seed_catalog(database_url: str, tenant_id: str, store_id: str) -> dict[str, str]:
    """往同一个 SQLite 文件里补商品、库存（8 箱）、箱->瓶换算、进价。

    登录会自动开租户，但**不会**建商品——商品/库存是用户自己录的，
    所以这里按真实用户建档的顺序补进去，再走语音链路。

    用 **ORM 模型**而不是裸 SQL：UUID 列在 SQLite 上落库是 32 位无短横线的 hex，
    裸 SQL 塞一个带短横线的字符串会「插得进去但查不出来」，是最典型的假联调陷阱。

    Returns:
        ``{"product_id": ..., "partner_id": ...}``
    """
    from app.db.session import Database
    from app.models.catalog import Product, UnitConversion
    from app.models.inventory import Inventory
    from app.models.partner import Partner

    database = Database.from_url(database_url)
    product_id = uuid.uuid4()
    partner_id = uuid.uuid4()
    tenant_uuid = uuid.UUID(tenant_id)
    store_uuid = uuid.UUID(store_id)
    try:
        with database.session(bypass_scope=True) as session:
            session.add(
                Product(
                    tenant_id=tenant_uuid,
                    product_id=product_id,
                    name="可乐",
                    spec="500ml",
                    base_unit="瓶",
                    last_purchase_price_fen=3500,
                    last_sale_price_fen=4000,
                )
            )
            session.add(
                Partner(
                    tenant_id=tenant_uuid,
                    partner_id=partner_id,
                    type="supplier",
                    name="老张",
                )
            )
            # 8 箱 × 24 瓶/箱 = 192 瓶（库存一律以**基本单位**存）
            session.add(
                Inventory(
                    tenant_id=tenant_uuid,
                    store_id=store_uuid,
                    product_id=product_id,
                    qty_base=Decimal("192"),
                    version=0,
                )
            )
            session.add(
                UnitConversion(
                    tenant_id=tenant_uuid,
                    product_id=product_id,
                    from_unit="箱",
                    to_unit="瓶",
                    ratio=Decimal("24"),
                    status="approved",
                )
            )
    finally:
        database.dispose()
    return {"product_id": str(product_id), "partner_id": str(partner_id)}


def read_inventory(database_url: str, tenant_id: str, product_id: str) -> tuple[Decimal, int]:
    """读库存（``qty_base`` + ``version``）。"""
    from sqlalchemy import select

    from app.db.session import Database
    from app.models.inventory import Inventory

    database = Database.from_url(database_url)
    try:
        with database.session(bypass_scope=True) as session:
            row = session.execute(
                select(Inventory.qty_base, Inventory.version).where(
                    Inventory.tenant_id == uuid.UUID(tenant_id),
                    Inventory.product_id == uuid.UUID(product_id),
                )
            ).one()
            return Decimal(str(row[0])), int(row[1])
    finally:
        database.dispose()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(report: Report) -> int:
    """跑完整链路；返回进程退出码。"""

    workdir = Path(tempfile.mkdtemp(prefix="e2e-voice-"))
    db_path = workdir / "e2e.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "WX_PROVIDER": "fake",
        "JWT_SECRET": "e2e-only-secret-not-for-production",
        "PYTHONPATH": str(SERVER_ROOT),
    }

    server: subprocess.Popen[bytes] | None = None
    log_path = workdir / "uvicorn.log"
    try:
        if port_in_use():
            print(f"[warn] {BASE_URL} 已有服务在跑，将直接复用（结果依赖那个进程的库，慎用）")
            boot_ms = 0.0
        else:
            prepare_database(database_url)
            log_file = log_path.open("wb")
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
                cwd=str(SERVER_ROOT),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            boot_ms = wait_until_up()
        report.stage("0. 服务启动（uvicorn）", boot_ms, "measured", "本地进程冷启动，生产是常驻容器")

        # ---- 登录（自动开租户；这也是端侧 wx.login 走的那条路）
        t0 = time.perf_counter()
        status, login = call_api("POST", "/api/v1/auth/login", body={"code": "e2e-dev-direct"})
        login_ms = (time.perf_counter() - t0) * 1000
        report.stage("1. 登录（wx.login → JWT）", login_ms)
        token = str(login.get("jwt") or "")
        tenant_id = str(login.get("tenant_id") or "")
        store_id = str(login.get("store_id") or "")
        report.check("登录拿到 JWT", token != "", f"HTTP {status}")
        if not token or not tenant_id or not store_id:
            print("登录失败，后续无法进行：", login)
            return 2

        # ---- 建档（真实用户先建商品/库存，再语音记账）
        t0 = time.perf_counter()
        seeded = seed_catalog(database_url, tenant_id, store_id)
        report.stage("2. 建档（商品/库存 8 箱/换算）", (time.perf_counter() - t0) * 1000, "measured", "只做一次的前置动作，不计入单次记账")
        before_qty, before_version = read_inventory(database_url, tenant_id, seeded["product_id"])
        report.check(
            "起始库存 = 8 箱（192 瓶）",
            before_qty == Decimal("192"),
            f"qty_base={before_qty} version={before_version}",
        )

        # ---- G0/G1 门禁 + 令牌桶 + 上传票据（端侧「按住说」后第一件事）
        t0 = time.perf_counter()
        status, session_payload = call_api(
            "POST",
            "/api/v1/voice/sessions",
            token=token,
            body={"duration_ms": 2_400, "mode": "hold", "energy_rms": 0.12, "vad_tail_ms": 300},
        )
        session_ms = (time.perf_counter() - t0) * 1000
        report.stage("3. G0-G2 门禁+令牌桶+票据", session_ms, "measured", "会话创建（含 1 次库存令牌桶）")
        session_id = str(session_payload.get("session_id") or "")
        report.check("会话创建成功", session_id != "", f"HTTP {status} state={session_payload.get('state')}")
        if session_id == "":
            print("会话创建失败：", session_payload)
            return 3

        # ---- ASR：本地没有转写服务，用 asr_text 直接喂文本，**如实标 skipped**
        report.stage(
            "4. ASR（语音转文本）",
            0.0,
            "skipped(no_asr_service)",
            "需要真实 AppID + 云托管 + ASR 三方；本链路直接用 asr_text，不当 0ms 喜报",
        )

        # ---- 解析（G3 规则层 → G4 → 预演 → 建卡；一次请求内完成）
        t0 = time.perf_counter()
        status, parsed = call_api(
            "POST",
            f"/api/v1/voice/sessions/{session_id}/parse",
            token=token,
            body={"asr_text": UTTERANCE, "asr_confidence": 0.95, "duration_ms": 2_400},
        )
        parse_ms = (time.perf_counter() - t0) * 1000
        route = str(parsed.get("route") or "")
        report.check("route = card（能直接出卡）", route == "card", f"route={route} reason={parsed.get('reason')}")
        report.check("规则层命中（rule_hit）", bool(parsed.get("rule_hit")) is True)
        report.check(
            "LLM 调用次数 C_llm = 0",
            int(parsed.get("llm_calls", -1)) == 0,
            f"llm_calls={parsed.get('llm_calls')}",
        )
        if route != "card":
            # 没出卡时把 trace 与澄清问题打出来——不然只看到一个 route 没法定位
            print("\n[debug] 未出卡，解析详情：")
            print("  trace:", parsed.get("trace"))
            print("  clarify:", parsed.get("clarify"))
            print("  parse_result:", json.dumps(parsed.get("parse_result"), ensure_ascii=False)[:1200])

        # parse 一次请求里混了 G3/G4/预演/建卡，用 trace 归因，而不是再切一刀请求
        report.stage("5. G3 规则层", 0.0, "attributed", "见 trace：规则命中，无 LLM")
        report.stage("6. G4 结构化", 0.0, "attributed", "规则层已给出完整槽位，未进 LLM")
        report.stage("7. 预演 + 建卡（含扣次）", parse_ms, "measured", "同一次 /parse 请求内完成")

        card = parsed.get("card") or {}
        lines = (card.get("calc") or {}).get("lines") or []
        total_fen = sum(int(line.get("amount_fen") or 0) for line in lines)
        report.check(
            "金额 = ¥70.00（2 箱 × 35 元）",
            total_fen == EXPECT_AMOUNT_FEN,
            f"total_fen={total_fen} lines={[(l.get('product_label'), l.get('quantity'), l.get('unit'), l.get('amount_fen')) for l in lines]}",
        )
        report.check("卡片 7 区块齐全", all(key in card for key in ("original", "intent", "slots", "calc", "impact", "risk", "actions", "meta")), ",".join(sorted(card.keys())))
        impact_rows = card.get("impact") or []
        report.check("影响预演非空（只读）", len(impact_rows) > 0, f"rows={len(impact_rows)}")

        direction_text = str(((card.get("intent") or {}).get("intent_label_zh")) or "")
        report.check(
            "方向 = 进货（不是卖货）",
            "进货" in direction_text or "采购" in direction_text,
            f"intent_label_zh={direction_text}",
        )

        # ---- 落账（确认卡片：CAS + 写单据 + 写流水）
        card_id = str((card.get("meta") or {}).get("card_id") or "")
        t0 = time.perf_counter()
        status, confirm_envelope = call_api(
            "POST",
            f"/api/v1/voice/cards/{card_id}/confirm",
            token=token,
            body={"confirm_type": "button"},
        )
        confirm_ms = (time.perf_counter() - t0) * 1000
        report.stage("8. 落账（CAS 确认）", confirm_ms, "measured", "唯一写入口：单据 + 流水 + 库存")
        # 真实结构是 ``{result, idempotency_key}``（见 app/api/v1/confirm.py），
        # 不是平铺的 outcome——端上 services/api.ts 的类型也按这个来。
        confirmed = confirm_envelope.get("result") or {}
        report.check(
            "落账成功",
            bool(confirmed.get("confirmed")) is True,
            f"doc_no={confirmed.get('doc_no')} detail={confirmed.get('detail')}",
        )
        if not confirmed.get("confirmed"):
            print("\n[debug] 落账未成功：", json.dumps(confirm_envelope, ensure_ascii=False)[:800])
            print("[debug] 卡片明细：", json.dumps(lines, ensure_ascii=False)[:800])

        after_qty, after_version = read_inventory(database_url, tenant_id, seeded["product_id"])
        report.check(
            "库存 8 箱 → 10 箱（192 → 240 瓶）",
            after_qty == Decimal("240"),
            f"before=192 after={after_qty}（{after_qty / 24} 箱）version {before_version}→{after_version}",
        )

        # ---- 端到端总耗时（不含一次性启动与建档）
        e2e_ms = session_ms + parse_ms + confirm_ms
        report.stage("— 端到端合计（3 + 7 + 8）", e2e_ms, "measured", "不含启动/建档/ASR（本地无 ASR）")
        report.check(
            f"端到端 ≤ {MAX_TOTAL_MS}ms",
            e2e_ms <= MAX_TOTAL_MS,
            f"{e2e_ms:.1f}ms",
        )

        # ---- 只读查询：证明账真的记进去了
        status, stock = call_api("GET", f"/api/v1/query/stock?keyword={quote('可乐')}", token=token)
        items = stock.get("items") or []
        report.check("只读查询能查到这笔进货后的库存", len(items) > 0, f"items={len(items)} HTTP {status}")

        # ---- LLM 成本：C_llm = 0 时不应产生任何模型调用记录
        status, usage_envelope = call_api("GET", "/api/v1/usage", token=token)
        # 真实结构是 ``{usage, ledger_rows, caliber_note}``（见 app/api/v1/usage.py）
        usage = usage_envelope.get("usage") or {}
        report.check(
            "本单未产生模型成本（cost_fen = 0）",
            int(usage.get("cost_fen") or 0) == 0,
            f"cost_fen={usage.get('cost_fen')} period={usage.get('period')} "
            f"ledger_rows={usage_envelope.get('ledger_rows')}",
        )

    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if log_path.is_file():
            tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
            if tail:
                print("\n[uvicorn 尾部日志]")
                for line in tail:
                    print("  " + line)
        shutil.rmtree(workdir, ignore_errors=True)

    return 0 if report.ok else 1


def main() -> int:
    """入口。"""
    report = Report()
    code = run(report)
    print()
    print(report.render())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
