"""G4 冲突检测：PRD §2.6 的 **12 类歧义**逐条落地（纯规则，不调 LLM）。

为什么要独立一层
----------------
冲突检测不是「再加几个 if」，而是**入账前的最后一道语义闸门**：
它拿到的已经是「意图 + 槽位」，职责是对这套结论做**反向质疑**——
「你说是进货，可原话说的是『还剩』，这是快照不是增量，退回去」。

12 类冲突（编号与 PRD §2.6 一一对应）
-------------------------------------
======  ==========================  ==================================================
编号    冲突                        本模块的处理口径
======  ==========================  ==================================================
C1      **盘点快照 vs 增量入库**     「还剩 N」= 覆盖式快照；「再入 N」= 增量。判反即**阻断**
C2      卖了 vs 退货                含「退」的写操作一律阻断，必须走退货意图 + R2 二次确认
C3      单位换算无系数              **绝不臆造**系数，标 ``pending`` 进待教学队列
C4      金额 vs 单价                 两者都给且不自洽 → 阻断；只给一个裸数字 → 追问
C5      同名商品 / 别名歧义         命中多个档案 → 阻断 + 候选澄清
C6      模糊时间                    「上次」「那天」无锚点 → 追问具体日期
C7      负数与撤销                  负数/「不要了」→ 阻断，改走撤销链路
C8      数字读法（幺/拐/勾、三五、一百二）  歧义 → 阻断并给候选；口语省尾 → 高亮确认
C9      付款状态矛盾                **以最后明确表述为准**并提示
C10     老价 / 老规矩               带出最近价并标 ``needs_confirm``
C11     同音词（帐/账、箱/香、件/见、退/推）  已自动纠正处数 → 降置信度并提示
C12     门店归属                    多门店未指明 → 追问；多店并存 → 阻断
======  ==========================  ==================================================

``blocked=True`` 的冲突**不允许进卡片**，只能追问或降级为待办；
这就是「省钱不省确认」在代码里的样子。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.voice.template_rule import SlotSpan

__all__ = [
    "CONFLICT_SPECS",
    "Conflict",
    "ConflictContext",
    "ConflictSpec",
    "blocking_conflicts",
    "detect",
    "detect_all",
    "spec_of",
]

#: 严重程度
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


@dataclass(frozen=True)
class ConflictSpec:
    """一类冲突的静态定义（编号 ↔ PRD 章节）。"""

    code: str
    prd_ref: str
    name_zh: str
    default_resolution: str


#: 12 类冲突的**单一登记表**（顺序即 PRD §2.6 顺序）
CONFLICT_SPECS: tuple[ConflictSpec, ...] = (
    ConflictSpec("C1", "PRD §2.6 ①", "盘点快照 vs 增量入库", "「还剩 N」按覆盖式快照处理，「再入 N」按增量处理，判反即阻断"),
    ConflictSpec("C2", "PRD §2.6 ②", "卖了 vs 退货", "含「退」的写操作阻断，改走退货意图并 R2 二次确认"),
    ConflictSpec("C3", "PRD §2.6 ③", "单位换算无系数", "不臆造系数，标 pending 进待教学队列"),
    ConflictSpec("C4", "PRD §2.6 ④", "金额 vs 单价", "两者都给且不自洽 → 阻断；裸数字 → 追问"),
    ConflictSpec("C5", "PRD §2.6 ⑤", "同名商品 / 别名歧义", "命中多个档案阻断并给 2~3 个候选"),
    ConflictSpec("C6", "PRD §2.6 ⑥", "模糊时间", "无锚点的相对时间追问具体日期"),
    ConflictSpec("C7", "PRD §2.6 ⑦", "负数与撤销", "负数阻断，改走撤销 / 反做单"),
    ConflictSpec("C8", "PRD §2.6 ⑧", "数字读法", "歧义给候选，口语省尾高亮确认"),
    ConflictSpec("C9", "PRD §2.6 ⑨", "付款状态矛盾", "以最后明确表述为准并提示"),
    ConflictSpec("C10", "PRD §2.6 ⑩", "老价 / 老规矩", "带出最近价并标 needs_confirm"),
    ConflictSpec("C11", "PRD §2.6 ⑪", "同音词", "自动纠正并降置信度"),
    ConflictSpec("C12", "PRD §2.6 ⑫", "门店归属", "多门店未指明 → 追问"),
)

_SPEC_BY_CODE: Mapping[str, ConflictSpec] = {spec.code: spec for spec in CONFLICT_SPECS}


def spec_of(code: str) -> ConflictSpec:
    """按编号取冲突定义（未登记直接 ``KeyError``，防止偷偷加第 13 类）。"""
    return _SPEC_BY_CODE[code]


@dataclass(frozen=True)
class Conflict:
    """一次实际发生的冲突。"""

    code: str
    severity: str
    message: str
    resolution: str
    blocked: bool = False
    clarify_slot: str | None = None
    options: tuple[str, ...] = ()

    @property
    def spec(self) -> ConflictSpec:
        """对应的静态定义。"""
        return spec_of(self.code)

    @property
    def name_zh(self) -> str:
        """中文名。"""
        return self.spec.name_zh


@dataclass
class ConflictContext:
    """检测输入（全部来自上游，不含任何网络/数据库依赖）。"""

    text: str
    intent_id: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    source: Mapping[str, str] = field(default_factory=dict)
    #: 数字解析歧义的片段（「三五」）
    ambiguous_spans: tuple[SlotSpan, ...] = ()
    #: ``amount_calculator`` 给出的判定规则名
    amount_rule: str = ""
    #: 商品命中多个档案
    product_ambiguous: bool = False
    product_options: tuple[str, ...] = ()
    #: 日期是相对时间且无锚点
    date_needs_confirm: bool = False
    #: ASR 同音纠正处数
    corrections: int = 0
    #: 租户内门店名（用于冲突 C12）
    store_names: tuple[str, ...] = ()
    #: 是否已提供单位换算系数
    has_ratio: bool = True


# --------------------------------------------------------------------------- 词表
_SNAPSHOT_MARKERS: tuple[str, ...] = ("还剩", "剩下", "剩", "还有", "数了", "数一下", "盘", "库存是", "一共剩")
_INCREMENT_MARKERS: tuple[str, ...] = ("再入", "再进", "又进", "又到", "补了", "补货", "新增", "加了", "又拿", "再来")
_RETURN_MARKERS: tuple[str, ...] = ("退", "换货", "退回")
_REVERSAL_MARKERS: tuple[str, ...] = ("撤销", "撤回", "不要了", "作废", "反做", "删掉")
#: 负数/减号标记（数字解析会把符号丢掉，只能在原文上找）。
#: 「减」单独一个字也收：口语里「减两箱」就是减库存，绝不能记成正向进货。
_NEGATIVE_MARKERS: tuple[str, ...] = ("负", "-", "－", "减", "少了两", "退掉")
_STALE_PRICE_MARKERS: tuple[str, ...] = ("老价", "老规矩", "按老", "上次那个价", "原价", "还是那个价", "照旧")
_STORE_MARKERS: tuple[str, ...] = ("店", "门市", "仓库", "分仓", "总仓")
_COLLOQUIAL_NUMBER = ("一百二", "两百三", "三百五", "一千二", "一万二", "两百五", "三百二")

#: 含「退/减/盘/剩」的常见商品名词（剔除后再做字面冲突检查，避免「卖两盒退热贴 / 进两个U盘」误阻断）
_COMPOUND_NOUN_EXCLUSIONS: tuple[str, ...] = (
    "退热贴",
    "退烧贴",
    "退烧药",
    "退黑素",
    "褪黑素",
    "减肥茶",
    "减肥药",
    "减脂",
    "减糖",
    "减盐",
    "u盘",
    "U盘",
    "优盘",
    "硬盘",
    "固态盘",
    "光盘",
    "键盘",
    "果盘",
    "托盘",
    "餐盘",
    "盘子",
    "棋盘",
    "蚊香盘",
    "剩菜",
    "剩饭",
)


def _strip_product_compounds(text: str) -> str:
    """做字面单字冲突检测前剔除含干扰字的商品名词。"""
    cleaned = text or ""
    for noun in _COMPOUND_NOUN_EXCLUSIONS:
        if noun in cleaned:
            cleaned = cleaned.replace(noun, "")
    return cleaned

#: 写类意图（负数/退货/撤销会对它们产生错账）
_WRITE_INTENTS: frozenset[str] = frozenset(
    {
        "purchase.create",
        "purchase.create_multi",
        "sales.create",
        "sales.create_multi",
        "stock.take",
        "stock.take_multi",
        "stock.loss",
        "purchase_return.create",
        "sales_return.create",
        "payment.receive",
        "payment.pay",
    }
)

_SNAPSHOT_INTENTS: frozenset[str] = frozenset({"stock.take", "stock.take_multi"})
_INCREMENT_INTENTS: frozenset[str] = frozenset(
    {"purchase.create", "purchase.create_multi", "sales.create", "sales.create_multi"}
)


# --------------------------------------------------------------------------- 检测器
def _detect_snapshot_vs_increment(ctx: ConflictContext) -> Conflict | None:
    """C1：最危险的一类——快照当增量会直接把库存记成两倍。"""
    text = _strip_product_compounds(ctx.text or "")
    has_snapshot = any(marker in text for marker in _SNAPSHOT_MARKERS)
    has_increment = any(marker in text for marker in _INCREMENT_MARKERS)
    if has_snapshot and has_increment:
        return Conflict(
            code="C1",
            severity=SEVERITY_HIGH,
            message="同一句里既有「还剩」又有「再入」，无法判断是覆盖还是累加",
            resolution="追问：这次是「盘点成 N」还是「又进了 N」",
            blocked=True,
            clarify_slot="Q",
            options=("按盘点覆盖", "按进货累加"),
        )
    if ctx.intent_id in _SNAPSHOT_INTENTS and has_increment:
        return Conflict(
            code="C1",
            severity=SEVERITY_HIGH,
            message=f"原话含增量表述（{_first_hit(text, _INCREMENT_MARKERS)}），但被判成盘点快照",
            resolution="按增量处理（转进货意图），不得覆盖现有库存",
            blocked=True,
        )
    if ctx.intent_id in _INCREMENT_INTENTS and has_snapshot:
        return Conflict(
            code="C1",
            severity=SEVERITY_HIGH,
            message=f"原话含快照表述（{_first_hit(text, _SNAPSHOT_MARKERS)}），但被判成增量入库/出库",
            resolution="按快照设定处理（转盘点意图），不得在现有库存上累加",
            blocked=True,
        )
    return None


def _detect_sale_vs_return(ctx: ConflictContext) -> Conflict | None:
    """C2：说「退」却记成「卖」是最常见的错账。"""
    text = _strip_product_compounds(ctx.text or "")
    if not any(marker in text for marker in _RETURN_MARKERS):
        return None
    sales_like = ctx.intent_id in {"sales.create", "sales.create_multi", "purchase.create", "purchase.create_multi"}
    if not sales_like:
        return None
    return Conflict(
        code="C2",
        severity=SEVERITY_HIGH,
        message=f"原话含退货语义（{_first_hit(text, _RETURN_MARKERS)}），不得记成正向进货/销售",
        resolution="改走退货意图（R2/C2 二次确认）",
        blocked=True,
    )


def _detect_unit_ratio_missing(ctx: ConflictContext) -> Conflict | None:
    """C3：没有已审核系数就必须 pending，禁止「常见值」兜底。

    判据以 ``source["U"] == "pending"`` 为准（这是 ``slot_filler`` 在**确认查不到系数**
    之后才打的标记）；``has_ratio`` 只是兜底信号，避免上游漏标时静默放过。
    """
    unit = ctx.slots.get("U")
    if unit is None:
        return None
    if ctx.source.get("U") != "pending" and ctx.has_ratio:
        return None
    return Conflict(
        code="C3",
        severity=SEVERITY_MEDIUM,
        message=f"单位「{unit}」没有已审核换算系数",
        resolution="标 pending 进待教学队列，卡片显示「待教学」，不得臆造系数",
        clarify_slot="RATIO",
    )


def _detect_amount_vs_unit_price(ctx: ConflictContext) -> Conflict | None:
    """C4：单价 vs 总价。两个都给且乘积不自洽 → 阻断。"""
    if ctx.amount_rule in {"both_hints", "bare_number_ambiguous"}:
        return Conflict(
            code="C4",
            severity=SEVERITY_HIGH,
            message="只听到一个金额，分不清是单价还是总价",
            resolution="追问「这是单价还是一共」，不猜",
            blocked=True,
            clarify_slot="UP",
            options=("按单价", "按总价"),
        )
    up = ctx.slots.get("UP")
    ta = ctx.slots.get("TA")
    qty = ctx.slots.get("Q")
    if isinstance(up, int) and isinstance(ta, int) and isinstance(qty, (int, float)) and qty:
        expected = up * qty
        if abs(expected - ta) > max(1, abs(ta) // 100):
            return Conflict(
                code="C4",
                severity=SEVERITY_HIGH,
                message=f"单价 × 数量 = {expected} 分，与总价 {ta} 分不一致",
                resolution="追问实际单价或总价，不得取其一",
                blocked=True,
                clarify_slot="UP",
            )
    return None


def _detect_product_alias(ctx: ConflictContext) -> Conflict | None:
    """C5：同名商品命中多个档案 → 必须澄清。"""
    if not ctx.product_ambiguous:
        return None
    return Conflict(
        code="C5",
        severity=SEVERITY_HIGH,
        message="商品名同时命中多个档案，无法唯一确定",
        resolution="给 2~3 个候选让用户点选，禁止默认取第一个",
        blocked=True,
        clarify_slot="P",
        options=ctx.product_options[:3],
    )


def _detect_fuzzy_date(ctx: ConflictContext) -> Conflict | None:
    """C6：相对时间无锚点 → 追问具体日期。"""
    if not ctx.date_needs_confirm:
        return None
    return Conflict(
        code="C6",
        severity=SEVERITY_MEDIUM,
        message="时间表述缺少锚点（如「上次」「那天」）",
        resolution="追问具体日期并显示解析结果，禁止用今天兜底",
        clarify_slot="D",
    )


def _detect_negative_or_reversal(ctx: ConflictContext) -> Conflict | None:
    """C7：负数 / 撤销语义不得进正常写链路。

    两道判据缺一不可：
    1. ``Q < 0``——只有上游把符号带下来才看得到；
    2. **原话里有「负 / - / 减」这类标记**——``number_normalizer`` 会把符号丢掉，
       只靠 ``Q < 0`` 就会把「进了负两箱可乐」解析成 +2 箱（**用户想减，系统加**）。
       实测过：「进了-2箱可乐」→ ``Q=2``、``qty_base=48``、``conflicts=[]``。
    """
    text = _strip_product_compounds(ctx.text or "")
    qty = ctx.slots.get("Q")
    if isinstance(qty, (int, float)) and qty < 0:
        return Conflict(
            code="C7",
            severity=SEVERITY_HIGH,
            message=f"数量为负数（{qty}）",
            resolution="负数不是入库方式，改走撤销 / 反做单",
            blocked=True,
        )
    if ctx.intent_id in _WRITE_INTENTS and any(marker in text for marker in _NEGATIVE_MARKERS):
        return Conflict(
            code="C7",
            severity=SEVERITY_HIGH,
            message=f"原话含负数标记（{_first_hit(text, _NEGATIVE_MARKERS)}）",
            resolution="不能把数量记成负数/减号：减库存请走退货或撤销（sys.undo），不得反向记账",
            blocked=True,
        )
    if ctx.intent_id in _WRITE_INTENTS and any(marker in text for marker in _REVERSAL_MARKERS):
        return Conflict(
            code="C7",
            severity=SEVERITY_HIGH,
            message=f"原话含撤销语义（{_first_hit(text, _REVERSAL_MARKERS)}）",
            resolution="改走 sys.undo 反做单链路，不得新增一张正向单",
            blocked=True,
        )
    return None


def _detect_number_reading(ctx: ConflictContext) -> Conflict | None:
    """C8：数字读法歧义（三五）与口语省尾（一百二）。"""
    if ctx.ambiguous_spans:
        span = ctx.ambiguous_spans[0]
        options = tuple(span.alternatives[:3]) or ("35", "3.5", "3 和 5")
        return Conflict(
            code="C8",
            severity=SEVERITY_HIGH,
            message=f"数字「{span.text}」有多种读法",
            resolution="给候选让用户点选，禁止默认取第一个",
            blocked=True,
            clarify_slot=span.code,
            options=options,
        )
    text = ctx.text or ""
    for marker in _COLLOQUIAL_NUMBER:
        if marker in text:
            return Conflict(
                code="C8",
                severity=SEVERITY_MEDIUM,
                message=f"口语省尾数字「{marker}」，需要高亮确认",
                resolution="按标准读法解析并在卡片上高亮数量，请用户核对",
                clarify_slot="Q",
            )
    return None


_PS_PAID: tuple[str, ...] = ("已付", "付了", "给了", "微信转", "转了", "结清", "付清", "现结")
_PS_UNPAID: tuple[str, ...] = ("欠着", "欠", "挂账", "记账", "没给", "未付", "赊账", "下回给")


def _detect_payment_conflict(ctx: ConflictContext) -> Conflict | None:
    """C9：付款状态矛盾 → 以最后明确表述为准。"""
    text = ctx.text or ""
    paid_positions = [(text.rfind(word), word) for word in _PS_PAID if word in text]
    unpaid_positions = [(text.rfind(word), word) for word in _PS_UNPAID if word in text]
    if not paid_positions or not unpaid_positions:
        return None
    last_paid = max(paid_positions)
    last_unpaid = max(unpaid_positions)
    winner = "已结" if last_paid[0] > last_unpaid[0] else "未结"
    winner_word = last_paid[1] if winner == "已结" else last_unpaid[1]
    return Conflict(
        code="C9",
        severity=SEVERITY_MEDIUM,
        message=f"付款状态前后矛盾（「{last_paid[1]}」与「{last_unpaid[1]}」同时出现）",
        resolution=f"以最后表述「{winner_word}」为准，记为 {winner}，卡片需提示",
        clarify_slot="PS",
    )


def _detect_stale_price(ctx: ConflictContext) -> Conflict | None:
    """C10：老价 / 老规矩 → 带出最近价但必须确认。"""
    text = ctx.text or ""
    marker = _first_hit(text, _STALE_PRICE_MARKERS)
    if marker is None:
        return None
    return Conflict(
        code="C10",
        severity=SEVERITY_MEDIUM,
        message=f"原话引用了历史价格（「{marker}」）",
        resolution="带出最近一次单价并标「按你的习惯」，needs_confirm=true",
        clarify_slot="UP",
    )


def _detect_homophone(ctx: ConflictContext) -> Conflict | None:
    """C11：同音纠正已发生 → 降置信度。"""
    if ctx.corrections <= 0:
        return None
    return Conflict(
        code="C11",
        severity=SEVERITY_LOW,
        message=f"同音字自动纠正 {ctx.corrections} 处（如 帐→账、箱→香）",
        resolution="保留纠正结果并降低置信度，卡片显示纠正明细",
    )


def _detect_store_attribution(ctx: ConflictContext) -> Conflict | None:
    """C12：门店归属。多店未指明 → 追问；多店并存 → 阻断。"""
    text = ctx.text or ""
    if "W" in ctx.slots:
        named = [name for name in ctx.store_names if name and name in text]
        if len(named) > 1:
            return Conflict(
                code="C12",
                severity=SEVERITY_HIGH,
                message=f"一句话提到多个门店：{'、'.join(named)}",
                resolution="一次只能记一个门店，追问确定",
                blocked=True,
                clarify_slot="W",
                options=tuple(named[:3]),
            )
        return None
    if len(ctx.store_names) > 1 and any(marker in text for marker in _STORE_MARKERS):
        return Conflict(
            code="C12",
            severity=SEVERITY_MEDIUM,
            message="租户有多个门店，原话未指明归属",
            resolution="追问门店，禁止跨店猜测或继承上次",
            clarify_slot="W",
            options=tuple(ctx.store_names[:3]),
        )
    return None


#: 编号 → 检测器（顺序 = 优先级，先报的更要紧）
_DETECTORS: tuple[tuple[str, Callable[[ConflictContext], Conflict | None]], ...] = (
    ("C1", _detect_snapshot_vs_increment),
    ("C2", _detect_sale_vs_return),
    ("C7", _detect_negative_or_reversal),
    ("C4", _detect_amount_vs_unit_price),
    ("C5", _detect_product_alias),
    ("C8", _detect_number_reading),
    ("C3", _detect_unit_ratio_missing),
    ("C12", _detect_store_attribution),
    ("C9", _detect_payment_conflict),
    ("C6", _detect_fuzzy_date),
    ("C10", _detect_stale_price),
    ("C11", _detect_homophone),
)


def _first_hit(text: str, markers: tuple[str, ...]) -> str | None:
    """返回第一个出现的标记词（按词表顺序，保证可复现）。"""
    for marker in markers:
        if marker in text:
            return marker
    return None


def detect(ctx: ConflictContext) -> tuple[Conflict, ...]:
    """按优先级跑全部 12 类检测器，返回实际发生的冲突。"""
    conflicts: list[Conflict] = []
    for _code, detector in _DETECTORS:
        found = detector(ctx)
        if found is not None:
            conflicts.append(found)
    return tuple(conflicts)


def detect_all(ctx: ConflictContext) -> Mapping[str, Conflict | None]:
    """按编号返回 ``{C1: Conflict|None, ...}``（回归测试逐类断言用）。"""
    return {code: detector(ctx) for code, detector in _DETECTORS}


def blocking_conflicts(conflicts: tuple[Conflict, ...]) -> tuple[Conflict, ...]:
    """筛出必须追问/不得出卡的冲突。"""
    return tuple(conflict for conflict in conflicts if conflict.blocked)
