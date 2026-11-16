"""意图 → 路由的**单一判定点**（H1 / H2 / H8 / 验收②）。

为什么单独一个模块
------------------
「这句话该出卡片、该直接答、还是该进待办」这件事，写在 API 里就是一段 if；
写在测试里就变成「测试自己再抄一遍 if」——抄的那份永远不会跟实现一起改。
所以判定只写一处 :func:`route_of_spec`，API 与测试**都调用它**，
`tests/test_route_matrix.py` 再把「路由结论」与「真实行为」双向对齐：

- 判定为 ``card`` 的意图，必须**真的能构造出 7 区块卡片**；
- 判定为 ``answer`` / ``static`` / ``pending_task`` 的意图，
  卡片构造器必须**拒绝**，落账服务必须**报对应错误码**。

四条路由（顺序即优先级，改顺序必须改本节文档）
-------------------------------------------
====================  ==================  ========  ========  ==================
route                 适用                写库      扣次      说明
====================  ==================  ========  ========  ==================
``static``            ``sys.help`` 等     否        否        本地话术，连查库都不用
``pending_task``      R3 白名单动作       只写待办  否        **没有语音直写旁路**
``answer``            R0 只读             否        否        查完就返回，不建卡
``card``              R1 / R2             是        是        唯一写库入口
====================  ==================  ========  ========  ==================

``clarify`` / ``manual`` 不是「意图」的路由，而是**同一条意图在对话中途的状态**
（问不清就追问，追问 2 轮还不清就切键盘），所以不在这张表里，
由 :mod:`app.voice.clarify_fsm` 单独负责。
"""

from __future__ import annotations

from typing import Mapping

from app.domain.intent_registry import IntentRegistry, IntentSpec, default_registry

__all__ = [
    "ALL_ROUTES",
    "ROUTE_ANSWER",
    "ROUTE_CARD",
    "ROUTE_CLARIFY",
    "ROUTE_MANUAL",
    "ROUTE_PENDING_TASK",
    "ROUTE_STATIC",
    "R0_ROUTE",
    "route_for_intent",
    "route_of_spec",
    "route_table",
    "route_writes_db",
    "route_bills_credit",
    "route_builds_card",
]

#: 静态意图：本地话术直接返回（不进 G3/G4，不查库）
ROUTE_STATIC = "static"
#: R0 只读：查完就返回（**不建卡片**，H2）
ROUTE_ANSWER = "answer"
#: R3 白名单：语音只生成待办（**不建可执行卡片**，H8）
ROUTE_PENDING_TASK = "pending_task"
#: R1/R2：出确认卡片 → 确认后落账（唯一写库入口，H1）
ROUTE_CARD = "card"
#: 澄清中（≤2 轮）——不是意图级路由，是会话中途状态
ROUTE_CLARIFY = "clarify"
#: 澄清用尽 → 切数字键盘补录（架构 ``CLARIFY_R2 --> MANUAL``）
ROUTE_MANUAL = "manual"

#: 意图级路由全集（不含 clarify/manual 这两个会话状态）
ALL_ROUTES: tuple[str, ...] = (
    ROUTE_STATIC,
    ROUTE_ANSWER,
    ROUTE_PENDING_TASK,
    ROUTE_CARD,
)

#: R0 只读意图的路由（静态意图优先命中 ``static``）
R0_ROUTE = ROUTE_ANSWER


def route_of_spec(spec: IntentSpec) -> str:
    """一个意图的最终路由（**判定顺序即优先级，不要调换**）。

    Args:
        spec: 意图规格（全部字段来自契约）。

    Returns:
        :data:`ALL_ROUTES` 之一。

    Notes:
        顺序：``static`` > ``pending_task`` > ``answer`` > ``card``。

        - ``static`` 优先，是因为 ``sys.help`` 这类连商品档案都不用查；
        - ``pending_task`` 优先于 ``answer``：R3 动作即使某天被标成 R0，
          也必须走待办而不是「查完就答」（否则等于给它开了直写旁路）。
    """
    if spec.is_static:
        return ROUTE_STATIC
    if spec.whitelist_blocked:
        return ROUTE_PENDING_TASK
    if spec.is_readonly:
        return R0_ROUTE
    return ROUTE_CARD


def route_for_intent(intent_id: str, registry: IntentRegistry | None = None) -> str:
    """按 intent_id 取路由（未登记意图直接抛 ``KeyError``，不猜）。

    Raises:
        KeyError: 契约里没有这个 intent。
    """
    reg = registry or default_registry()
    return route_of_spec(reg.require(intent_id))


def route_table(registry: IntentRegistry | None = None) -> Mapping[str, str]:
    """全部意图的路由表（``{intent_id: route}``，按契约 ``seq`` 排序）。"""
    reg = registry or default_registry()
    return {spec.intent_id: route_of_spec(spec) for spec in reg.all()}


def route_writes_db(route: str) -> bool:
    """该路由是否写业务库（``pending_task`` 只写待办，不算写业务库）。"""
    return route in {ROUTE_CARD}


def route_bills_credit(route: str) -> bool:
    """该路由是否扣次——**只有出卡片才扣**，一次语音最多扣一次。"""
    return route == ROUTE_CARD


def route_builds_card(route: str) -> bool:
    """该路由是否应当构造确认卡片（其他路由构造卡片一律视为越权）。"""
    return route == ROUTE_CARD
