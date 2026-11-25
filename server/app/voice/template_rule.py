"""G3 规则模板层：**槽位片段抽取器** + **意图规则表** + **匹配器**（全程 0 LLM）。

本模块是「规则层 = 最大省钱点」的落地点（架构 §4 ③）：只要这里命中，
整条语音链路**一次模型都不调**，成本为 0。

三层职责划分
------------
1. **抽取器**（``extract_*``）：从原话里切出 ``Q/U/UP/TA/AMT/PS/D/RATIO/TF/ALIAS/DOC``
   的候选片段。只做**确定性文本解析**，不做意图判断、不猜、不兜底。
2. **规则表**（``RULE_TABLE``）：每个 intent 一组「必须命中的关键词组」（``groups``，
   组内 OR、组间 AND）+ 否定词 + 结构前置条件（要不要数量、要不要疑问词…）。
3. **匹配器**（``match_rules``）：按 ``priority`` 降序 + 证据强度排序，取最优。

关键词表的来源与诚实性声明
--------------------------
规则表按 **intent 语义**（``name_zh`` / ``domain`` / ``required_slots``）与零售口语常识
人工编写，**匹配时不读取 ``sample_utterances``**。回归集只用于事后度量
（见 ``tests/test_template_hit.py``），不参与匹配——否则「用样本命中样本」的
自证循环会让命中率数字失去意义。

优先级链（团队已拍）
--------------------
``危险语义（盘点快照 / 退货 / 金额改动）`` 优先 → ``静态系统意图`` →
``高频模板（18 个 template_hit）`` → 未命中交 G4。
退货类语义通过 ``none_keywords`` 反向保护：任何含「退」的原话都不得被
采购/销售模板吞掉，否则会把「退货」记成「进货」，是最危险的一类错账。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from app.utils.date_utils import resolve_relative_date
from app.utils.unit import CANONICAL_UNITS, normalize_unit
from app.voice.direction import (
    NEUTRAL_TRADE_WORDS,
    PURCHASE_DIRECTION_WORDS,
    SALES_DIRECTION_WORDS,
)
from app.voice.number_normalizer import NumberParse, parse_quantity

__all__ = [
    "PRIORITY_DANGER",
    "PRIORITY_FEEDBACK",
    "PRIORITY_QUERY",
    "PRIORITY_SYS_CONTROL",
    "PRIORITY_WRITE",
    "RULE_TABLE",
    "RuleMatch",
    "SlotSpan",
    "TemplateRule",
    "best_match",
    "extract_alias_span",
    "extract_date_span",
    "extract_doc_ref_span",
    "extract_note_span",
    "extract_payment_span",
    "extract_quantity_spans",
    "extract_ratio_span",
    "extract_target_field_span",
    "is_interrogative",
    "match_rules",
    "split_items",
]

# --------------------------------------------------------------------------- 优先级
#: 危险语义（盘点快照设定 / 退货 / 单位换算·金额类白名单动作）：压过一切通用模板
PRIORITY_DANGER = 900
#: 反馈/纠正类（「别记了」「记错了」）：必须压过业务模板，否则会多记一单
PRIORITY_SYS_CONTROL = 950
#: 查询类（库存 / 单据 / 欠款 / 报表）
PRIORITY_QUERY = 600
#: 写操作通用模板（进货 / 销售 / 损耗）
PRIORITY_WRITE = 700
#: 档案类（商品 / 别名）
PRIORITY_FEEDBACK = 500

#: 疑问词（判定「是不是在查」）。**不用 LLM 判断**，纯词表。
INTERROGATIVE_MARKERS: tuple[str, ...] = (
    "多少",
    "几",
    "够不够",
    "有没有",
    "还剩多少",
    "是不是",
    "吗",
    "呢",
    "啥",
    "什么",
    "哪",
    "如何",
    "怎",
)

#: 数量+单位片段（中文数字 / 阿拉伯数字 + 已登记单位）
_UNIT_ALTERNATION = "|".join(CANONICAL_UNITS)
_NUMBER_PATTERN = r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万幺拐勾俩仨壹贰叁肆伍陆柒捌玖拾佰仟]+"
_QUANTITY_PATTERN = re.compile(
    rf"(?P<num>{_NUMBER_PATTERN})\s*(?P<unit>{_UNIT_ALTERNATION})"
)

#: 付款状态词表（「以最后明确表述为准」由 ``conflict_detector`` 负责，这里只取最后一处）
_UNPAID_WORDS: tuple[str, ...] = ("欠着", "欠", "挂账", "记账", "没给", "未付", "赊账", "下回给", "以后给", "先欠")
_PAID_WORDS: tuple[str, ...] = ("已付", "付了", "给了", "微信转", "转了", "现金给", "结清", "付清", "现结")
_PARTIAL_WORDS: tuple[str, ...] = ("部分", "先付一半", "给了部分")

#: 目标字段词表（``TF`` 六值枚举）
_TARGET_FIELD_WORDS: tuple[tuple[str, str], ...] = (
    ("进价", "进价"),
    ("售价", "售价"),
    ("零售价", "售价"),
    ("卖价", "售价"),
    ("联系人", "联系人"),
    ("电话", "电话"),
    ("手机号", "电话"),
    ("名称", "名称"),
    ("名字", "名称"),
    ("备注", "备注"),
)

#: 别名线索词（``product.alias.add`` / ``sys.teach``）
_ALIAS_CUES: tuple[str, ...] = ("别名", "又叫", "就指", "就叫", "简称", "以后我说", "以后说", "以后")

#: 单据引用线索
_DOC_CUES: tuple[str, ...] = ("上一单", "上一张", "刚才那张", "那张单", "那张单子", "单据", "单子", "小票", "订单号", "单号")

#: 换算线索
_RATIO_PATTERN = re.compile(
    rf"(?P<outer>{_NUMBER_PATTERN})?\s*(?P<outer_unit>{_UNIT_ALTERNATION})?\s*"
    rf"(?:等于|=|是|合|换)\s*(?P<inner>{_NUMBER_PATTERN})\s*(?P<inner_unit>{_UNIT_ALTERNATION})"
)


# --------------------------------------------------------------------------- 片段
@dataclass(frozen=True)
class SlotSpan:
    """一个槽位候选片段（原文定位 + 结构化值 + 来源标注）。"""

    code: str
    text: str
    start: int
    end: int
    value: Any = None
    source: str = "asr"
    confidence: float = 0.0
    note: str = ""
    #: 歧义时的可选项（「三五」→ 35 / 3.5 / 3、5），禁止直接取第一个
    alternatives: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        """是否解析出歧义（必须澄清，不得猜）。"""
        return self.value is None and bool(self.alternatives)

    @property
    def ok(self) -> bool:
        """是否可用于填槽。"""
        return self.value is not None


def is_interrogative(text: str) -> bool:
    """是否在提问（纯词表判定，**不调 LLM**）。"""
    return any(marker in (text or "") for marker in INTERROGATIVE_MARKERS)


def split_items(text: str) -> tuple[str, ...]:
    """按「、」「，」「和」「以及」「还有」切分成多商品片段（保序去空）。"""
    if not text:
        return ()
    parts = re.split(r"[、,，;；]|以及|還有|还有|和(?![其正])", text)
    return tuple(part.strip() for part in parts if part and part.strip())


def extract_quantity_spans(text: str, *, allow_zero: bool = False) -> tuple[SlotSpan, ...]:
    """抽取 ``Q`` 与 ``U``（**必须有单位**才算数量，避免把金额当数量）。

    Args:
        text: 原话（或其片段）。
        allow_zero: 盘点/损耗允许 0（PRD 槽位校验）。

    Returns:
        ``(Q 片段, U 片段)``；无「数字+单位」时返回空元组——调用方应视为
        「没听清数量」而不是猜测。
    """
    if not text:
        return ()
    span = _first_quantity(text, allow_zero=allow_zero)
    return span


def _first_quantity(text: str, *, allow_zero: bool) -> tuple[SlotSpan, ...]:
    """取第一个「数字+单位」片段。"""
    match = _QUANTITY_PATTERN.search(text)
    if match is None:
        return ()
    raw_num = match.group("num")
    raw_unit = match.group("unit")
    unit = normalize_unit(raw_unit or "") or (raw_unit or "")
    parsed: NumberParse = parse_quantity(raw_num)
    if parsed.value is None:
        # 「三五」这类歧义：不猜，交给 G5 追问（PRD 2.6 #8）
        quantity_span = SlotSpan(
            code="Q",
            text=match.group(0),
            start=match.start(),
            end=match.end(),
            value=None,
            source="asr",
            confidence=parsed.confidence,
            note="数量读法有歧义",
            alternatives=parsed.alternatives,
        )
    else:
        value = int(parsed.value) if parsed.value == parsed.value.to_integral_value() else parsed.value
        if value < 0 or (value == 0 and not allow_zero):
            quantity_span = SlotSpan(
                code="Q",
                text=match.group(0),
                start=match.start(),
                end=match.end(),
                value=None,
                source="asr",
                confidence=0.3,
                note=f"数量 {value} 不合法",
            )
        else:
            quantity_span = SlotSpan(
                code="Q",
                text=match.group(0),
                start=match.start(),
                end=match.end(),
                value=value,
                source="asr",
                confidence=parsed.confidence,
                note=parsed.trace,
            )
    unit_span = SlotSpan(
        code="U",
        text=raw_unit,
        start=match.start("unit"),
        end=match.end("unit"),
        value=unit,
        source="asr",
        confidence=0.95 if unit in CANONICAL_UNITS else 0.5,
    )
    return (quantity_span, unit_span)


def extract_payment_span(text: str) -> SlotSpan | None:
    """抽取 ``PS``（付款状态）：**取最后出现的明确表述**（PRD 2.6 #9）。"""
    if not text:
        return None
    candidates: list[tuple[int, str, str]] = []
    for word in _PARTIAL_WORDS:
        position = text.rfind(word)
        if position >= 0:
            candidates.append((position, "部分", word))
    for word in _UNPAID_WORDS:
        position = text.rfind(word)
        if position >= 0:
            candidates.append((position, "未结", word))
    for word in _PAID_WORDS:
        position = text.rfind(word)
        if position >= 0:
            candidates.append((position, "已结", word))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    position, value, word = candidates[-1]
    return SlotSpan(
        code="PS",
        text=word,
        start=position,
        end=position + len(word),
        value=value,
        source="asr",
        confidence=0.9,
        note=f"付款状态取自最后表述「{word}」",
    )


def extract_date_span(
    text: str,
    *,
    today_iso: str,
    last_date_iso: str | None = None,
) -> SlotSpan | None:
    """抽取 ``D``（业务日期）：相对时间必须解析成具体 ``YYYY-MM-DD``。"""
    if not text:
        return None
    cues = (
        "今天",
        "今儿",
        "今日",
        "昨天",
        "昨日",
        "前天",
        "明天",
        "后天",
        "上月",
        "上个月",
        "本月",
        "这个月",
        "上周",
        "这周",
        "上周五",
        "上次",
        "刚才",
        "上一单",
    )
    for cue in cues:
        position = text.find(cue)
        if position < 0:
            continue
        resolved = resolve_relative_date(cue, today_iso, last_date_iso)
        if resolved.date is None:
            return SlotSpan(
                code="D",
                text=cue,
                start=position,
                end=position + len(cue),
                value=None,
                source="asr",
                confidence=0.3,
                note="相对时间缺少锚点，必须确认",
            )
        return SlotSpan(
            code="D",
            text=cue,
            start=position,
            end=position + len(cue),
            value=resolved.date,
            source="asr",
            confidence=0.9 if not resolved.needs_confirm else 0.6,
            note=resolved.matched_rule,
        )
    match = re.search(r"(?P<day>\d{1,2})\s*(?:号|日)", text)
    if match is not None:
        resolved = resolve_relative_date(match.group(0), today_iso, last_date_iso)
        if resolved.date is not None:
            return SlotSpan(
                code="D",
                text=match.group(0),
                start=match.start(),
                end=match.end(),
                value=resolved.date,
                source="asr",
                confidence=0.85,
            )
    return None


def extract_ratio_span(text: str) -> SlotSpan | None:
    """抽取 ``RATIO``（换算系数）：「一箱等于 24 瓶」→ 24。

    **只从原话取，不臆造**；取不到就返回 ``None``，由 ``unit_converter`` 走 ``pending``。
    """
    if not text:
        return None
    match = _RATIO_PATTERN.search(text)
    if match is None:
        return None
    parsed = parse_quantity(match.group("inner") or "")
    if parsed.value is None:
        return SlotSpan(
            code="RATIO",
            text=match.group(0),
            start=match.start(),
            end=match.end(),
            value=None,
            source="asr",
            confidence=parsed.confidence,
            note="换算系数读法有歧义",
            alternatives=parsed.alternatives,
        )
    ratio = Decimal(parsed.value)
    if ratio <= 0:
        return None
    return SlotSpan(
        code="RATIO",
        text=match.group(0),
        start=match.start(),
        end=match.end(),
        value=float(ratio),
        source="asr",
        confidence=0.9,
        note=parsed.trace,
    )


def extract_target_field_span(text: str) -> SlotSpan | None:
    """抽取 ``TF``（目标字段）：六值枚举，取最先出现的一个。"""
    if not text:
        return None
    best: tuple[int, str, str] | None = None
    for word, field_name in _TARGET_FIELD_WORDS:
        position = text.find(word)
        if position < 0:
            continue
        if best is None or position < best[0]:
            best = (position, field_name, word)
    if best is None:
        return None
    position, field_name, word = best
    return SlotSpan(
        code="TF",
        text=word,
        start=position,
        end=position + len(word),
        value=field_name,
        source="asr",
        confidence=0.9,
    )


_ALIAS_QUOTED = re.compile(r"[「『\"'‘’](?P<alias>[^」』\"'‘’]{1,30})[」』\"'‘’]")


def extract_alias_span(text: str) -> SlotSpan | None:
    """抽取 ``ALIAS``（别名）：优先取引号内的说法，其次取线索词之后的名词串。"""
    if not text:
        return None
    quoted = _ALIAS_QUOTED.search(text)
    if quoted is not None:
        return SlotSpan(
            code="ALIAS",
            text=quoted.group("alias"),
            start=quoted.start("alias"),
            end=quoted.end("alias"),
            value=quoted.group("alias").strip(),
            source="asr",
            confidence=0.92,
        )
    for cue in _ALIAS_CUES:
        position = text.find(cue)
        if position < 0:
            continue
        tail = text[position + len(cue) :].strip(" ：:，,。.、")
        if not tail:
            continue
        return SlotSpan(
            code="ALIAS",
            text=tail,
            start=position + len(cue),
            end=position + len(cue) + len(tail),
            value=tail[:30],
            source="asr",
            confidence=0.75,
            note=f"取自线索词「{cue}」之后",
        )
    return None


def extract_doc_ref_span(text: str) -> SlotSpan | None:
    """抽取 ``DOC``（单据引用）：必须带具体线索词，禁止凭空造引用。"""
    if not text:
        return None
    for cue in _DOC_CUES:
        position = text.find(cue)
        if position >= 0:
            return SlotSpan(
                code="DOC",
                text=cue,
                start=position,
                end=position + len(cue),
                value=cue,
                source="asr",
                confidence=0.8,
                note="需在 T04 绑定为具体 doc_no + 日期后展示",
            )
    return None


def extract_note_span(text: str) -> SlotSpan | None:
    """抽取 ``N``（备注）：显式「备注：xxx」之后的内容。"""
    if not text:
        return None
    match = re.search(r"备注[：:]\s*(?P<note>.+)$", text)
    if match is None:
        return None
    note = match.group("note").strip()[:200]
    return SlotSpan(
        code="N",
        text=note,
        start=match.start("note"),
        end=match.start("note") + len(note),
        value=note,
        source="asr",
        confidence=0.9,
    )


# --------------------------------------------------------------------------- 规则
@dataclass(frozen=True)
class TemplateRule:
    """一条 G3 规则：关键词组 + 结构前置条件 + 优先级。"""

    rule_id: str
    intent_id: str
    priority: int
    #: 组间 AND、组内 OR；空元组表示无词法要求（只靠结构条件）
    groups: tuple[tuple[str, ...], ...] = ()
    #: 命中任一即**否决**（退货保护、纠正保护）
    none_keywords: tuple[str, ...] = ()
    require_interrogative: bool = False
    forbid_interrogative: bool = False
    require_quantity: bool = False
    require_money: bool = False
    #: 必须是「一句话多商品」才命中（``*_multi`` 意图专用）
    require_multi: bool = False
    #: 句子长度上限（控制类短指令用，避免「取消」吞掉整句业务）
    max_chars: int | None = None
    min_chars: int = 0
    note: str = ""

    def matched_groups(self, text: str) -> tuple[tuple[str, ...], ...]:
        """返回每个关键词组的命中词（未命中的组为空元组）。"""
        result: list[tuple[str, ...]] = []
        for group in self.groups:
            hits = tuple(word for word in group if word in text)
            result.append(hits)
        return tuple(result)

    def evidence(self, text: str) -> tuple[int, tuple[str, ...]]:
        """证据强度：``(命中的组数, 命中词总长)``。"""
        total = 0
        words: list[str] = []
        for hits in self.matched_groups(text):
            if hits:
                total += 1
                words.extend(hits)
        return total, tuple(words)


@dataclass(frozen=True)
class RuleMatch:
    """一次规则命中（含可解释证据，便于排查误判）。"""

    rule: TemplateRule
    score: float
    keywords: tuple[str, ...] = ()
    span_len: int = 0

    @property
    def intent_id(self) -> str:
        """命中的意图。"""
        return self.rule.intent_id

    def explain(self) -> str:
        """一行人类可读的解释（日志/测试断言用）。"""
        words = "/".join(self.keywords) if self.keywords else "<结构条件>"
        return f"{self.intent_id} <- {words} (priority={self.rule.priority}, score={self.score:.2f})"


def _r(
    intent_id: str,
    priority: int,
    groups: Sequence[Sequence[str]],
    *,
    rule_id: str | None = None,
    **kwargs: Any,
) -> TemplateRule:
    """构造规则的小工具（保持规则表可读）。"""
    return TemplateRule(
        rule_id=rule_id or f"rule.{intent_id}.{priority}",
        intent_id=intent_id,
        priority=priority,
        groups=tuple(tuple(group) for group in groups),
        **kwargs,
    )


#: 退货/纠正保护词（任何写模板都不得吞掉这些语义）
_REVERSAL_GUARD: tuple[str, ...] = ("退回", "退货", "退给", "退款", "退他", "退她", "退了", "退我", "不要在", "不要了")

#: 采购侧识别词 = 方向唯一的进货词 + **方向中性**的交易动词。
#: 中性词（要/要了/拿了/到了）只负责「认出这是一笔交易」，
#: 方向由 :mod:`app.voice.direction` 单独判（明确方向词 > partner.type > 收付信号 > 追问）。
_PURCHASE_KEYWORDS: tuple[str, ...] = PURCHASE_DIRECTION_WORDS + NEUTRAL_TRADE_WORDS

#: 销售侧识别词 = 方向唯一的销售词 + **同一批**中性词。
#: 两侧在中性词上必须**完全对称**，否则「要十件」这类话会直接漏到 G4 花钱；
#: 对称性是硬约束，由 ``tests/test_direction_resolution.py`` 断言守着。
_SALES_KEYWORDS: tuple[str, ...] = SALES_DIRECTION_WORDS + NEUTRAL_TRADE_WORDS

#: G3 规则表：**只覆盖 18 个 ``template_hit=true`` 的意图**。
#: 其余 20 个意图一律走 G4（LLM），因为它们的语义/槽位不适合纯模板。
RULE_TABLE: tuple[TemplateRule, ...] = (
    # ---------------- 静态系统意图（本地内容，直接返回，不调 LLM） ----------------
    _r(
        "sys.help",
        PRIORITY_SYS_CONTROL,
        (("帮助", "你能听懂", "怎么用", "会什么", "教我", "说明一下"),),
        rule_id="rule.sys.help",
        max_chars=24,
        note="静态话术本地拼装",
    ),
    _r(
        "sys.example",
        PRIORITY_SYS_CONTROL,
        (("举个例子", "举例", "说一句", "示范", "演示一下"),),
        rule_id="rule.sys.example",
        max_chars=24,
        note="静态话术本地拼装",
    ),
    # ---------------- 控制类短指令（必须压过业务模板） ----------------
    _r(
        "sys.cancel",
        PRIORITY_SYS_CONTROL,
        (("取消", "别记", "不记了", "重来", "算了", "不用记"),),
        rule_id="rule.sys.cancel",
        max_chars=8,
        note="只允许短指令，防止吞掉整句业务",
    ),
    _r(
        "sys.continue",
        PRIORITY_SYS_CONTROL,
        (("接着录", "继续录", "录完了", "接着来", "连续录", "再录一个"),),
        rule_id="rule.sys.continue",
        max_chars=10,
    ),
    # ---------------- 危险语义：库存快照（最危险，必须优先） ----------------
    _r(
        "stock.take_multi",
        PRIORITY_DANGER + 10,
        (("盘点", "盘货", "盘了", "盘下", "盘一下", "数了下", "数一下", "点数", "清点", "还剩", "还有", "剩下"),),
        rule_id="rule.stock.take_multi",
        require_multi=True,
        none_keywords=_REVERSAL_GUARD,
        require_quantity=True,
        forbid_interrogative=True,
        note="多商品快照：一句话多个 SKU",
    ),
    _r(
        "stock.take",
        PRIORITY_DANGER,
        (("盘点", "盘货", "盘了", "盘下", "盘一下", "数了下", "数一下", "点数", "清点", "还剩", "还有", "剩下"),),
        rule_id="rule.stock.take",
        none_keywords=_REVERSAL_GUARD,
        require_quantity=True,
        forbid_interrogative=True,
        note="**快照设定**：覆盖式为「还剩 N」，不是增量",
    ),
    # ---------------- 损耗（R2：不能当进货/销售） ----------------
    _r(
        "stock.loss",
        PRIORITY_WRITE + 150,
        (("报损", "损耗", "坏了", "少了", "对不上", "短少", "破了", "烂了", "过期了", "摔了"),),
        rule_id="rule.stock.loss",
        none_keywords=_REVERSAL_GUARD,
        require_quantity=True,
    ),
    # ---------------- 单据汇总（先于单条查询与报表） ----------------
    _r(
        "doc.query_summary",
        PRIORITY_QUERY + 20,
        (
            ("一共", "总共", "合计", "加一起"),
            ("进了", "进货", "采购", "卖了", "销售", "出库", "赚了", "收入"),
        ),
        rule_id="rule.doc.query_summary",
        require_interrogative=True,
    ),
    # ---------------- 查询类 ----------------
    _r(
        "stock.query",
        PRIORITY_QUERY,
        (("还剩", "还有", "剩下", "库存", "够不够", "剩多少", "存货"),),
        rule_id="rule.stock.query",
        none_keywords=("补货", "快没了", "没了", "低库存", "流水", "进出", "卖了几", "进出多少"),
        require_interrogative=True,
        note="排除低库存/流水类（那两类走 LLM 兜底）",
    ),
    _r(
        "doc.query",
        PRIORITY_QUERY,
        (
            ("单子", "单据", "上一单", "上一张", "那张单", "那张", "小票", "单号", "的单"),
            ("调出来", "给我看", "查", "看一下", "翻出来", "打开"),
        ),
        rule_id="rule.doc.query",
        none_keywords=("记错", "改一下", "改过来", "改称", "撤销"),
    ),
    _r(
        "debt.query",
        # 比通用查询高 10：句子里出现「欠/应收/应付/挂账」时，方向是**明确**的账簿语义，
        # 不该被 stock.query 的泛化词（「还有」）抢走——「应收还有多少没收回来」
        # 曾被判成库存查询，这是「答非所问」级错误。
        PRIORITY_QUERY + 10,
        (("欠", "应收", "应付", "挂账"),),
        rule_id="rule.debt.query",
        require_interrogative=True,
        note="只查询；「还了 200」属收付款，走 G4",
    ),
    _r(
        "report.today",
        PRIORITY_QUERY,
        (
            ("今天", "今儿", "今日"),
            ("赚", "卖", "进", "收入", "营业额", "毛利"),
        ),
        rule_id="rule.report.today",
        none_keywords=("一共", "总共", "合计", "卖了几", "进出", "流水", "查一下"),
        require_interrogative=True,
        note="含「一共」时归 doc.query_summary；流水类走 LLM",
    ),
    # ---------------- 写操作通用模板 ----------------
    _r(
        "purchase.create_multi",
        PRIORITY_WRITE + 20,
        (_PURCHASE_KEYWORDS,),
        rule_id="rule.purchase.create_multi",
        none_keywords=_REVERSAL_GUARD + ("以后", "我说"),
        require_quantity=True,
        require_multi=True,
        forbid_interrogative=True,
    ),
    _r(
        "purchase.create",
        PRIORITY_WRITE,
        (_PURCHASE_KEYWORDS,),
        rule_id="rule.purchase.create",
        none_keywords=_REVERSAL_GUARD + ("以后", "我说", "进价", "售价"),
        require_quantity=True,
        forbid_interrogative=True,
    ),
    _r(
        "sales.create_multi",
        PRIORITY_WRITE + 20,
        (_SALES_KEYWORDS,),
        rule_id="rule.sales.create_multi",
        require_multi=True,
        none_keywords=_REVERSAL_GUARD
        + (
            "卖得",
            "最好",
            "最多",
            "排行",
            "畅销",
            "下架",
            "别卖",
            "以后",
            "我说",
            "不要",
            "需要",
            "只要",
            "主要",
            "加个新品",
            "新品",
            "新增商品",
            "建档",
            "进价",
            "售价",
            "零售价",
            "改价",
            "调到",
            "改到",
        ),
        require_quantity=True,
        forbid_interrogative=True,
    ),
    _r(
        "sales.create",
        PRIORITY_WRITE,
        (_SALES_KEYWORDS,),
        rule_id="rule.sales.create",
        none_keywords=_REVERSAL_GUARD
        + (
            "卖得",
            "最好",
            "最多",
            "排行",
            "畅销",
            "下架",
            "别卖",
            "以后",
            "我说",
            "不要",
            "需要",
            "只要",
            "主要",
            "加个新品",
            "新品",
            "新增商品",
            "建档",
            "进价",
            "售价",
            "零售价",
            "改价",
            "调到",
            "改到",
        ),
        require_quantity=True,
        forbid_interrogative=True,
    ),
    # ---------------- 档案类 ----------------
    _r(
        "product.create",
        PRIORITY_FEEDBACK,
        (("加个新品", "新增商品", "新品", "建档", "加个商品", "新建商品"),),
        rule_id="rule.product.create",
    ),
    _r(
        "product.alias.add",
        PRIORITY_FEEDBACK,
        (("别名", "就指", "又叫", "就叫", "简称", "以后"),),
        rule_id="rule.product.alias.add",
        none_keywords=("改叫", "并到", "以后我说", "就是", "教我说", "别卖", "不卖", "下架"),
        note="「改叫」属 partner.update，「并到」属 partner.merge",
    ),
)


# --------------------------------------------------------------------------- 匹配
def _structural_ok(
    rule: TemplateRule,
    text: str,
    *,
    quantity_present: bool,
    money_present: bool,
    length: int,
    multi_present: bool = False,
) -> bool:
    """结构前置条件（不含关键词）是否满足。"""
    if rule.require_multi and not multi_present:
        return False
    if rule.max_chars is not None and length > rule.max_chars:
        return False
    if length < rule.min_chars:
        return False
    if rule.require_quantity and not quantity_present:
        return False
    if rule.require_money and not money_present:
        return False
    interrogative = is_interrogative(text)
    if rule.require_interrogative and not interrogative:
        return False
    if rule.forbid_interrogative and interrogative:
        return False
    return True


def match_rules(
    text: str,
    *,
    rules: Iterable[TemplateRule] = RULE_TABLE,
    quantity_present: bool | None = None,
    money_present: bool = False,
    multi_present: bool = False,
    strip_hint: str | None = None,
) -> tuple[RuleMatch, ...]:
    """把原话匹配到规则表，返回**按优先级降序**的候选（可能多个）。

    Args:
        text: 已归一化的原话。
        rules: 规则表（测试可注入子集）。
        quantity_present: 是否含「数字+单位」；``None`` 表示现场判断。
        money_present: 是否含金额片段。
        multi_present: 是否「一句话多商品」（由引擎按商品命中数判定）。
        strip_hint: 已识别为商品名的片段（用于把商品词从证据里剔除，避免误算长度）。

    Returns:
        命中的 :class:`RuleMatch` 序列（已排序）；空元组表示 G3 未命中。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ()
    haystack = cleaned.replace(strip_hint, "") if strip_hint else cleaned
    if quantity_present is None:
        quantity_present = bool(extract_quantity_spans(cleaned))
    length = len(cleaned)

    matches: list[RuleMatch] = []
    for rule in rules:
        if any(word in cleaned for word in rule.none_keywords):
            continue
        if not _structural_ok(
            rule,
            haystack,
            quantity_present=quantity_present,
            money_present=money_present,
            length=length,
            multi_present=multi_present,
        ):
            continue
        groups_hit, words = rule.evidence(haystack)
        if rule.groups and groups_hit < len(rule.groups):
            continue
        span_len = sum(len(word) for word in words)
        # 证据分：组数优先，其次关键词总长；归一化到 0~1 便于和 LLM 置信度对比
        score = min(1.0, 0.55 + 0.2 * groups_hit + 0.02 * span_len)
        matches.append(
            RuleMatch(rule=rule, score=round(score, 4), keywords=words, span_len=span_len)
        )

    matches.sort(key=lambda item: (item.rule.priority, item.score, item.span_len), reverse=True)
    return tuple(matches)


def best_match(matches: Sequence[RuleMatch]) -> RuleMatch | None:
    """取最优命中（已排序，取第一个）。"""
    return matches[0] if matches else None
