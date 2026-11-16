"""交易**方向**判定：把「进货 / 卖货」从关键词匹配里拆出来单独判（0 LLM）。

为什么要有这个模块（团队已拍的方案 d）
--------------------------------------
T03 的规则表把方向押在关键词上，但里面有四个词**方向中性**：

    要 / 要了 / 拿了 / 到了

「客户要十件」是销售，「我要进货十箱」是进货；「客户拿走两箱」是销售，
「我去拿货」是进货。**方向由主语和场景决定，不由这几个字决定。**
把它们塞进某一侧就必然产生「记错账」类误判（held-out H03/H08/H11），
而这类错误的代价是「账错了」，不是「多花一次模型钱」——两个代价差两个数量级。

因此本模块按团队给的判据优先级做判定，**只用确定性信号，一次模型都不调**：

    明确方向词（方向唯一） > partner.type > 收付信号 > 都没有则强制澄清

冲突处理（团队明确要求）
------------------------
任一类判据给出方向、且**互相矛盾**时不判（例如「进货」+ 客户名），
走澄清并提示「听出矛盾了」。宁可贵一句，不许猜。

``partner.type`` 判据为什么 0 成本
---------------------------------
上游把客户与供应商分别编成两个 ``AliasResolver``（``customers`` / ``suppliers``），
所以「命中哪个词表」本身就是 ``partner.type``：

* 只命中客户表 → 销售；
* 只命中供应商表 → 进货；
* 两张表都命中（同一家既是客户又是供应商）→ **不判**，交给澄清。

不需要新表、不需要查库、不需要模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.voice.output_schema import ClarifyPrompt

__all__ = [
    "CLARIFY_ONLY_SLOTS",
    "DIRECTION_SLOT",
    "NEUTRAL_TRADE_WORDS",
    "PURCHASE",
    "PURCHASE_DIRECTION_WORDS",
    "SALES",
    "SALES_DIRECTION_WORDS",
    "DirectionVerdict",
    "build_direction_clarify",
    "direction_of_intent",
    "is_trade_intent",
    "resolve_direction",
    "switch_trade_intent",
]

#: 方向取值（与 ``side_effect.inventory_delta`` 语义一一对应：increase / decrease）
PURCHASE = "purchase"
SALES = "sales"

#: G5 追问用的**伪槽位**：``DIR`` 不在 ``slots.json`` 的 18 槽位里，
#: 不落库、不进 ``ParseResult.slots``，只作为「这一轮问的是什么」的标签。
DIRECTION_SLOT = "DIR"

#: 允许出现在 ``ClarifyPrompt.slot`` 但**不属于** 18 槽位的伪槽位集合。
CLARIFY_ONLY_SLOTS: frozenset[str] = frozenset({DIRECTION_SLOT})

#: 方向**唯一**的进货词（只留不会指向销售的说法）
PURCHASE_DIRECTION_WORDS: tuple[str, ...] = (
    "进货",
    "进点货",
    "进了",
    "采购",
    "上货",
    "提货",
    "拿货",
    "补货",
    "补了",
    "再入",
    "又进",
    "拉到",
    "拉了",
)

#: 方向**唯一**的销售词
SALES_DIRECTION_WORDS: tuple[str, ...] = (
    "卖了",
    "卖货",
    "卖",
    "零售",
    "出货",
    "出库",
    "提走",
    "拿走",
    "发出",
)

#: **方向中性**的交易动词：只参与「这是一笔交易」的识别，**绝不参与方向判定**。
#: 这几个词在规则表里两侧都要出现（对称），否则「要十件」会直接漏到 G4。
NEUTRAL_TRADE_WORDS: tuple[str, ...] = (
    "要",
    "要了",
    "拿了",
    "到了",
)

#: 「对方给钱」类信号（我方**收钱**）→ 销售。
#: 只认「给(我) + 金额」这种明确表述；「给了他」是付钱，见下一张表。
_RECEIVE_MONEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:给了|给|付了|付)\s*(?:我\s*)?[\d零〇一二两三四五六七八九十百千万]+"),
    re.compile(r"(?:微信|支付宝|现金|扫码)?\s*转(?:给)?我"),
    re.compile(r"收(?:了|到)(?:钱|款|账)"),
    re.compile(r"现结|当场付|当场给|付清了|结清了|一次付清"),
)

#: 「我方向对方给钱」类信号（我方**付钱**）→ 进货。
#: 必须出现「给/付/结 + 他|她|对方|老X」这种明确的受事对象，否则不算。
_PAY_MONEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:结|付|还|打|转|给)\s*(?:了|给)?\s*(?:他|她|人家|对方|老板|老[\u4e00-\u9fa5]{1,2})"),
    re.compile(r"付了?\s*(?:货款|进货款)"),
)

#: 交易类意图 ↔ 方向（``side_effect.inventory_delta`` 的语义映射）
_INTENT_DIRECTION: dict[str, str] = {
    "purchase.create": PURCHASE,
    "purchase.create_multi": PURCHASE,
    "sales.create": SALES,
    "sales.create_multi": SALES,
}

#: 单商品 ↔ 多商品 的镜像表（换方向时保留 ``_multi`` 后缀）
_MIRROR: dict[str, str] = {
    "purchase.create": "sales.create",
    "sales.create": "purchase.create",
    "purchase.create_multi": "sales.create_multi",
    "sales.create_multi": "purchase.create_multi",
}


@dataclass(frozen=True)
class DirectionVerdict:
    """一次方向判定的结论（含人可读理由，便于排查误判）。"""

    #: ``purchase`` / ``sales`` / ``None``（判不出来）
    direction: str | None
    #: 判据来源：``strong_word`` / ``partner_type`` / ``money_signal`` / ``none``
    reason: str
    #: 命中的证据（原话里的词或 partner 名）
    evidence: tuple[str, ...] = ()
    #: 判据之间是否互相矛盾（矛盾时不判，走澄清）
    conflict: bool = False

    @property
    def resolved(self) -> bool:
        """是否判出了方向。"""
        return self.direction is not None

    def explain(self) -> str:
        """一行人类可读的解释（写进 ``calc_traces``，排查误判时直接看得到）。"""
        if self.conflict:
            return f"方向判据矛盾 {list(self.evidence)} → 不判，追问方向"
        if self.direction is None:
            return "方向判据不足（无明确方向词 / 无已知往来对象 / 无收付信号）→ 追问方向"
        side = "进货" if self.direction == PURCHASE else "卖货"
        return f"方向={side}（依据={self.reason}，证据={list(self.evidence)}）"


def is_trade_intent(intent_id: str) -> bool:
    """是否「进货 / 销售」这类**有方向**的意图。"""
    return intent_id in _INTENT_DIRECTION


def direction_of_intent(intent_id: str) -> str | None:
    """意图自带的方向（非交易类返回 ``None``）。"""
    return _INTENT_DIRECTION.get(intent_id)


def switch_trade_intent(intent_id: str, direction: str | None) -> str | None:
    """按方向把意图换到对侧（保留 ``_multi`` 后缀）。

    Args:
        intent_id: 当前意图（非交易类直接返回 ``None``）。
        direction: ``purchase`` / ``sales`` / ``None``。

    Returns:
        对侧意图；非交易类或方向为空时返回 ``None``（调用方保持原意图）。
        方向与意图已经一致时**返回原意图**（幂等，不会把对侧又翻回来）。
    """
    if direction not in (PURCHASE, SALES):
        return None
    current = _INTENT_DIRECTION.get(intent_id)
    if current is None:
        return None
    if current == direction:
        return intent_id
    return _MIRROR.get(intent_id)


def _hits(text: str, words: tuple[str, ...]) -> tuple[str, ...]:
    """返回原话里出现的词（顺序按词表，便于稳定断言）。"""
    return tuple(word for word in words if word in text)


def _first_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """返回第一个命中的模式片段（用于证据展示）。"""
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None


def resolve_direction(
    text: str,
    *,
    has_customer: bool = False,
    has_supplier: bool = False,
    customer_names: tuple[str, ...] = (),
    supplier_names: tuple[str, ...] = (),
) -> DirectionVerdict:
    """判定这句话的方向（**纯函数**，不查库、不调模型）。

    判据优先级（团队已拍）：明确方向词 > ``partner.type`` > 收付信号 > 都不判。

    Args:
        text: 归一化后的原话。
        has_customer: 句中是否出现已知**客户**。
        has_supplier: 句中是否出现已知**供应商**。
        customer_names: 命中的客户名（只用于证据展示）。
        supplier_names: 命中的供应商名（只用于证据展示）。

    Returns:
        :class:`DirectionVerdict`；``direction is None`` 表示必须追问方向。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return DirectionVerdict(direction=None, reason="empty_text")

    # ---- 判据一：方向唯一的强方向词
    purchase_hits = _hits(cleaned, PURCHASE_DIRECTION_WORDS)
    sales_hits = _hits(cleaned, SALES_DIRECTION_WORDS)
    strong: str | None = None
    strong_evidence: tuple[str, ...] = ()
    if purchase_hits and sales_hits:
        # 同一句里进货词和销售词都出现（「进了两箱，卖了一箱」）→ 不判
        return DirectionVerdict(
            direction=None,
            reason="strong_word_conflict",
            evidence=purchase_hits[:2] + sales_hits[:2],
            conflict=True,
        )
    if purchase_hits:
        strong, strong_evidence = PURCHASE, purchase_hits[:2]
    elif sales_hits:
        strong, strong_evidence = SALES, sales_hits[:2]

    # ---- 判据二：partner.type（命中哪张词表就是什么类型）
    partner: str | None = None
    partner_evidence: tuple[str, ...] = ()
    if has_customer and has_supplier:
        partner = None  # 同一家既是客户又是供应商 → 不判
        partner_evidence = tuple(list(customer_names[:1]) + list(supplier_names[:1]))
    elif has_customer:
        partner, partner_evidence = SALES, customer_names[:1] or ("客户",)
    elif has_supplier:
        partner, partner_evidence = PURCHASE, supplier_names[:1] or ("供应商",)

    # ---- 判据三：收付信号
    receive = _first_match(cleaned, _RECEIVE_MONEY_PATTERNS)
    pay = _first_match(cleaned, _PAY_MONEY_PATTERNS)
    money: str | None = None
    money_evidence: tuple[str, ...] = ()
    if receive and not pay:
        money, money_evidence = SALES, (receive,)
    elif pay and not receive:
        money, money_evidence = PURCHASE, (pay,)
    elif receive and pay:
        money = None  # 既收又付，判不了

    # ---- 合成：任一类给出方向就比，矛盾则一律不判
    signals = [
        (side, reason, evidence)
        for side, reason, evidence in (
            (strong, "strong_word", strong_evidence),
            (partner, "partner_type", partner_evidence),
            (money, "money_signal", money_evidence),
        )
        if side is not None
    ]
    if not signals:
        if has_customer and has_supplier:
            reason = "partner_type_ambiguous"
        elif receive and pay:
            reason = "money_signal_conflict"
        else:
            reason = "none"
        return DirectionVerdict(direction=None, reason=reason)

    directions = {side for side, _reason, _evidence in signals}
    if len(directions) > 1:
        # 判据互相矛盾（「进货」+ 客户名；客户名 + 付钱给对方）：不判
        merged: list[str] = []
        for _side, reason, evidence in signals:
            merged.extend(f"{reason}:{word}" for word in evidence)
        return DirectionVerdict(
            direction=None, reason="criteria_conflict", evidence=tuple(merged), conflict=True
        )

    side, reason, evidence = signals[0]
    return DirectionVerdict(direction=side, reason=reason, evidence=evidence)


def build_direction_clarify(*, conflict: bool = False) -> ClarifyPrompt:
    """方向追问话术（**静态**，2 个按钮，0 LLM）。

    Args:
        conflict: 判据是否互相矛盾（矛盾时话术要说明「听出矛盾」）。

    Returns:
        :class:`ClarifyPrompt`，``slot`` 是伪槽位 ``DIR``。
    """
    question = (
        "这句我听出矛盾了，确认一下：是进货还是卖货？"
        if conflict
        else "这是进货还是卖货？点一下就行。"
    )
    return ClarifyPrompt(slot=DIRECTION_SLOT, question=question, options=["进货", "卖货"])
