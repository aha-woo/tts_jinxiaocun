"""在命令行「说一句话」，看它出卡、确认、落账。**仅本地开发用。**

为什么不用小程序点录音
----------------------
小程序首页的录音链是「录音 → 直传对象存储 → 只把 ``audio_object_ref`` 交给后端」，
本地既没有云开发对象存储、也没有真实 ASR，所以那条路在本机走不通
（会卡在上传/识别）。本脚本绕开录音，直接把「这句话识别出来的文字」交给同一个
``POST /voice/sessions/{id}/parse`` 接口 —— 后面全部是真的：真路由、真建卡、
真扣次、真写库、真库存。

用法::

    # 后端要先起着：uvicorn app.main:app --port 8000
    cd server
    python scripts/demo_say.py "进了两箱可乐，一件三十五"
    python scripts/demo_say.py "进了两箱可乐，一件三十五" --yes      # 顺手确认
    python scripts/demo_say.py                    # 交互模式，一句一句说
    python scripts/demo_say.py --base http://127.0.0.1:8001

交互模式里输 ``q`` 退出；卡出来后输 ``y`` 确认。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_BASE = "http://127.0.0.1:8000"
#: 本地登录用的假 code（WX_PROVIDER=fake 时任意非空即可）
LOGIN_CODE = "demo"


def _fmt_money(fen: object) -> str:
    """分 → 「¥12.50」。"""
    try:
        return f"¥{int(fen) / 100:.2f}"
    except (TypeError, ValueError):
        return "—"


def _card_summary(card: dict) -> list[str]:
    """把卡片缩成几行人在终端里能看懂的东西。"""
    intent = card.get("intent") or {}
    risk = card.get("risk") or {}
    out = [
        f"  意图 {intent.get('intent_label_zh') or intent.get('intent_id') or '?'}"
        f"（{intent.get('intent_id', '?')}，风险 {intent.get('risk_level', '?')}）"
    ]
    if risk.get("reason_zh"):
        out.append(f"  {risk['reason_zh']}")
    slots = [s for s in (card.get("slots") or []) if s.get("display")]
    if slots:
        out.append("  槽位 " + "  ".join(f"{s['code']}={s['display']}" for s in slots))
    for line in ((card.get("calc") or {}).get("lines")) or []:
        if line.get("quantity") in (None, ""):
            # 纯实体意图（教说法/改价）没有数量，明细行只是占位，别拿出来糊弄人
            continue
        flag = "（待确认）" if line.get("pending") else ""
        out.append(
            f"  明细 {line.get('product_label')} "
            f"{line.get('quantity')}{line.get('unit') or ''} "
            f"×{_fmt_money(line.get('unit_price_fen'))} "
            f"= {_fmt_money(line.get('amount_fen'))}{flag}"
        )
    #: 影响预览 = 确认后会发生什么（只读预演，不是真的已经改了）
    for row in card.get("impact") or []:
        out.append(
            f"  将会变化 {row.get('label')}：{row.get('before')} → {row.get('after')}"
        )
    pending = [s["code"] for s in slots if s.get("source") == "pending"]
    if pending:
        out.append(f"  黄色待确认项：{', '.join(pending)}（可选的不影响确认）")
    return out


def _print_result(body: dict) -> None:
    """打印确认回执。"""
    result = (body.get("data") or {}).get("result") or {}
    if not result:
        print(f"  没成功：{body.get('message')}  {json.dumps(body.get('data'), ensure_ascii=False)}")
        return
    if result.get("doc_no"):
        print(f"  已落账：单号 {result['doc_no']}   金额 {_fmt_money(result.get('total_amount_fen'))}")
    if result.get("summary"):
        print(f"  {result['summary']}")
    inv = result.get("inventory_after") or {}
    if inv:
        print("  库存变化：" + "  ".join(f"{v}" for v in inv.values()))
    if result.get("follow_up"):
        print(f"  下一步：{result['follow_up'].get('hint_zh')}")


def say(client: httpx.Client, base: str, headers: dict[str, str], text: str) -> dict:
    """建会话 → 解析，返回 ``data``。"""
    session = client.post(
        f"{base}/api/v1/voice/sessions",
        json={"duration_ms": 2_000, "energy_rms": 0.5},
        headers=headers,
    )
    session.raise_for_status()
    session_id = (session.json().get("data") or {}).get("session_id")
    if not session_id:
        raise SystemExit(f"没建起会话：{session.text}")
    parsed = client.post(
        f"{base}/api/v1/voice/sessions/{session_id}/parse",
        json={"asr_text": text},
        headers=headers,
    )
    parsed.raise_for_status()
    return parsed.json().get("data") or {}


def handle(client: httpx.Client, base: str, headers: dict[str, str], text: str, *, auto_yes: bool) -> None:
    """说一句 → 按 route 分流打印 → 需要时确认。"""
    data = say(client, base, headers, text)
    route = data.get("route")
    intent = (data.get("parse_result") or {}).get("intent")

    if route == "card":
        card = data.get("card") or {}
        print("\n【卡片】" + ("（已扣 1 次）" if data.get("credit_consumed") else ""))
        for line in _card_summary(card):
            print(line)
        if data.get("next_step_zh"):
            print(f"  注意：{data['next_step_zh']}")
        card_id = (card.get("meta") or {}).get("card_id")
        if auto_yes:
            answer = "y"
        else:
            answer = input("  确认记账？(y/N) ").strip().lower()
        if answer in {"y", "yes", "是"}:
            body = client.post(
                f"{base}/api/v1/voice/cards/{card_id}/confirm",
                json={"confirm_type": "button"},
                headers=headers,
            ).json()
            _print_result(body)
        else:
            client.post(
                f"{base}/api/v1/voice/cards/{card_id}/cancel",
                json={"reason": "user_cancel"},
                headers=headers,
            )
            print("  已取消（不落账）")
        return

    if route == "answer":
        answer = data.get("answer") or {}
        report = answer.get("report") or {}
        if report:
            print(
                f"\n【直接回答】不建卡、不扣次：今日采购 {report.get('purchase_display')}"
                f"，销售 {report.get('sales_display')}"
            )
        else:
            print(f"\n【直接回答】{json.dumps(answer, ensure_ascii=False)[:200]}")
        return

    if route == "clarify":
        clarify = data.get("clarify") or {}
        print(f"\n【要澄清】{clarify.get('question')}")
        print("  可点选项：" + " / ".join(clarify.get("options") or []))
        return

    if route == "pending_task":
        print("\n【R3 待办】该动作只能在电脑版微信大屏管理页由老板确认执行")
        print(f"  任务：{json.dumps(data.get('task'), ensure_ascii=False)[:160]}")
        return

    if route == "manual":
        print(f"\n【切键盘补录】{data.get('message_zh')}")
        return

    if route == "rejected":
        print(f"\n【拦下了】{data.get('error_code')}（不扣次）")
        return

    print(f"\n【没听懂】route={route} 原因={data.get('reason')} 错误码={data.get('error_code')}")
    if data.get("reason") == "schema_invalid":
        print("  本地 LLM 是 fake（没有真模型），这类「必须靠模型理解」的意图在本地会走不通。")
        print("  规则层能直接命中的说法（进货/卖货/查报表/别名/新建商品）才跑得通。")


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="命令行「说一句话」看效果（仅本地）")
    parser.add_argument("text", nargs="*", help="要说的话；不传则进交互模式")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"后端地址（默认 {DEFAULT_BASE}）")
    parser.add_argument("--yes", action="store_true", help="出卡后自动确认（不问）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    client = httpx.Client(timeout=30)
    try:
        login = client.post(f"{base}/api/v1/auth/login", json={"code": LOGIN_CODE})
        login.raise_for_status()
        data = login.json().get("data") or {}
        headers = {"Authorization": f"Bearer {data['jwt']}"}
        print(f"已登录（租户 {str(data.get('tenant_id'))[:8]}）→ {base}")
    except httpx.HTTPError as exc:
        print(f"连不上后端 {base}：{exc}\n先起后端：cd server && uvicorn app.main:app --port 8000")
        return 1

    text = " ".join(args.text).strip()
    if text:
        handle(client, base, headers, text, auto_yes=args.yes)
        return 0

    print("交互模式：说一句话回车提交，输 q 退出。")
    print("可以试：进了两箱可乐，一件三十五 / 卖了五瓶可乐，一瓶两块五 / 今天卖了多少")
    while True:
        try:
            line = input("\n你说：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in {"q", "quit", "exit"}:
            break
        handle(client, base, headers, line, auto_yes=args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
