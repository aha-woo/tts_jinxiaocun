"""G2 非命令过滤器（0 成本，架构 §4 ② / D5）。

判定链（**短路**，任一命中即返回，**全程不调 LLM**）
---------------------------------------------------
1. 无有效汉字 / 纯语气词 → 拒绝（``E_NON_COMMAND``）
2. 闲聊关键词表 → 拒绝（``E_NON_COMMAND``）
3. 静态意图 ``sys.help`` / ``sys.example`` → 本地内容直接返回（不进 G3/G4）
4. 60s 相同文本去重 → 复用上次结果（``E_FILTERED_DUP``，HTTP 200）
5. 业务动词白名单命中 → 放行 G3
6. 其余 → **放行**（宁可放行、不可误杀）

为什么第 6 步是放行
------------------
闲聊判定失败最多浪费一次 ASR；误杀命令则**直接丢一笔账**。两者代价不对称，
所以阈值必须偏松（D5 原文：「宁可放行、不可误杀」）。漏放的闲聊由 G3/G4 兜底
（G3 未命中且 LLM 也说不出意图 → 才判无意图，且**不扣次**）。

另外：**绝不用 LLM 判断「是不是命令」**——用 LLM 判闲聊等于为省钱先花钱（D5 否决项）。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "BUSINESS_VERBS",
    "CHITCHAT_KEYWORDS",
    "FILLER_CHARS",
    "STATIC_INTENT_KEYWORDS",
    "FilterDecision",
    "DedupStore",
    "MemoryDedupStore",
    "IntentCooldown",
    "NonCommandFilter",
    "text_hash",
]

#: 纯语气词/填充词（只有这些字符 -> 不是命令）
FILLER_CHARS: str = "嗯啊哦呃唉哎呀吧呢嘛哈噢诶诶嘿唉哟喔诺咯啦嗷嗯呐"

#: 闲聊关键词（明确不是记账/查询诉求）
CHITCHAT_KEYWORDS: tuple[str, ...] = (
    "你好",
    "您好",
    "在吗",
    "吃了没",
    "吃了吗",
    "天气",
    "今天天气",
    "早上好",
    "中午好",
    "晚上好",
    "晚安",
    "再见",
    "拜拜",
    "谢谢",
    "多谢",
    "辛苦了",
    "哈哈",
    "笑死",
    "无聊",
    "讲个笑话",
    "唱首歌",
    "你是谁",
    "你叫什么",
)

#: 静态意图关键词（本地话术直接返回，不调 LLM）
STATIC_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sys.help": ("帮助", "怎么用", "怎么使用", "能做什么", "会什么", "能干什么", "你能听懂", "教我", "使用说明"),
    "sys.example": ("举个例子", "举例", "说一句", "示范", "样例", "怎么说的", "我听听"),
}

#: 业务动词白名单（与 intents.json 的领域语义对应；新增说法时**加这里**即可）
BUSINESS_VERBS: tuple[str, ...] = (
    # 采购/入库
    "进",
    "进货",
    "进了",
    "进点",
    "入",
    "入库",
    "到货",
    "到了",
    "拉",
    "拉了",
    "拿",
    "拿了",
    "补",
    "补货",
    "采购",
    # 销售/出库
    "卖",
    "卖了",
    "售",
    "售出",
    "出库",
    "拿走",
    "发走",
    "要",
    "要给",
    "记账",
    "收了",
    # 退货
    "退",
    "退了",
    "退回",
    "退回来",
    "退货",
    # 库存
    "盘点",
    "盘",
    "盘了",
    "实盘",
    "数了下",
    "数了",
    "还剩",
    "还有",
    "库存",
    "够不够",
    "损耗",
    "报损",
    "坏了",
    "少了",
    "对不上",
    "快没了",
    "该补",
    # 查询/报表
    "查",
    "查一下",
    "多少",
    "几箱",
    "几件",
    "几个",
    "哪些",
    "进出",
    "卖得最好",
    "最多",
    "营业额",
    "赚了多少",
    "报表",
    "导出",
    # 往来/收付
    "欠",
    "还欠",
    "还了",
    "转了",
    "付给",
    "结了",
    "抹个零",
    "不要了",
    # 档案
    "新品",
    "加个",
    "新增",
    "别名",
    "也叫",
    "就指",
    "改成",
    "改叫",
    "调价",
    "进价",
    "售价",
    "零售价",
    "下架",
    "别卖了",
    "等于",
    "备注",
    "货号",
    "并到",
    # 系统
    "撤销",
    "撤回",
    "取消",
    "别记了",
    "重来",
    "接着录",
    "录完了",
    "改一下",
    "记错",
    "以后我说",
    "以后",
    "帮助",
    "举个例子",
)

#: 有效汉字范围（CJK 统一表意文字）
_HANZI_PATTERN = re.compile(r"[\u4e00-\u9fff]")

#: 标点/空白（判「纯语气词」时先剔除）
_NOISE_PATTERN = re.compile(r"[\s，,。.、！!？?：:；;“”\"'（）()【】\[\]…—~·]+")


def text_hash(text: str) -> str:
    """文本哈希（60s 去重键）。"""
    return hashlib.sha256(_NOISE_PATTERN.sub("", text or "").encode("utf-8")).hexdigest()


class DedupStore(Protocol):
    """60s 去重协议（Redis ``SETNX EX 60`` / 内存实现同语义）。"""

    def begin(self, key: str) -> bool:
        """占用键：``True`` = 首次（继续处理），``False`` = 60s 内重复。"""


class MemoryDedupStore:
    """内存去重（无 Redis 时的正常降级模式，不是故障）。"""

    def __init__(self, ttl_seconds: int = 60, max_entries: int = 4096) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_entries))
        self._data: dict[str, float] = {}

    def begin(self, key: str) -> bool:
        """返回是否为首次（超 TTL 视为首次）。"""
        now = time.monotonic()
        expired = [item for item, deadline in self._data.items() if deadline <= now]
        for item in expired:
            self._data.pop(item, None)
        if key in self._data:
            return False
        if len(self._data) >= self._max:
            oldest = min(self._data, key=lambda item: self._data[item])
            self._data.pop(oldest, None)
        self._data[key] = now + self._ttl
        return True

    def size(self) -> int:
        """当前条目数（测试断言用）。"""
        return len(self._data)


class IntentCooldown:
    """连续 N 次无意图 -> 短暂冷却（PRD 5.7 假设，阈值可配）。"""

    def __init__(self, limit: int = 5, ttl_seconds: int = 60) -> None:
        self._limit = max(1, int(limit))
        self._ttl = max(1, int(ttl_seconds))
        self._counter: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def record_no_intent(self, session_key: str) -> bool:
        """记录一次「无意图」；返回是否进入冷却。"""
        now = time.monotonic()
        count = self._counter.get(session_key, 0) + 1
        self._counter[session_key] = count
        if count >= self._limit:
            self._until[session_key] = now + self._ttl
            self._counter[session_key] = 0
            return True
        return False

    def record_hit(self, session_key: str) -> None:
        """命中一次有效命令 -> 计数清零。"""
        self._counter.pop(session_key, None)
        self._until.pop(session_key, None)

    def is_cooling(self, session_key: str) -> bool:
        """是否处于冷却期。"""
        deadline = self._until.get(session_key)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._until.pop(session_key, None)
            return False
        return True


@dataclass(frozen=True)
class FilterDecision:
    """G2 判定结果。

    ``action``：
    - ``reject``：拒绝（``E_NON_COMMAND``）
    - ``static``：静态意图，本地内容直接返回（``intent_id`` 有效）
    - ``duplicate``：60s 内重复（``E_FILTERED_DUP``，HTTP 200，复用上次结果）
    - ``pass``：疑似命令，放行 G3
    """

    action: str
    reason: str
    intent_id: str | None = None
    error_code: str | None = None
    matched: str | None = None
    confidence: float = 1.0

    @property
    def should_stop(self) -> bool:
        """是否终止流水线（不进 G3）。"""
        return self.action != "pass"


class NonCommandFilter:
    """G2 判定器（规则、可解释、可回归、0 成本）。"""

    def __init__(
        self,
        dedup: DedupStore | None = None,
        cooldown: IntentCooldown | None = None,
    ) -> None:
        self._dedup = dedup if dedup is not None else MemoryDedupStore()
        self._cooldown = cooldown if cooldown is not None else IntentCooldown()

    def check(self, text: str, *, session_key: str = "default") -> FilterDecision:
        """按固定顺序短路判定。

        Args:
            text: ASR 原文（已做同音纠偏）。
            session_key: 会话键（用于冷却计数，避免跨租户串味由调用方保证）。

        Returns:
            :class:`FilterDecision`
        """
        raw = (text or "").strip()
        compact = _NOISE_PATTERN.sub("", raw)

        # 1. 无有效汉字 / 纯语气词
        if not _HANZI_PATTERN.search(compact) and not re.search(r"\d", compact):
            return FilterDecision("reject", "no_chinese", error_code="E_NON_COMMAND")
        if compact and all(char in FILLER_CHARS for char in compact):
            return FilterDecision("reject", "filler_only", error_code="E_NON_COMMAND")

        # 2. 闲聊关键词表
        for keyword in CHITCHAT_KEYWORDS:
            if keyword in compact:
                return FilterDecision("reject", "chitchat", matched=keyword, error_code="E_NON_COMMAND")

        # 3. 静态意图（本地内容，不调 LLM）
        for intent_id, keywords in STATIC_INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in compact:
                    return FilterDecision("static", "static_intent", intent_id=intent_id, matched=keyword)

        # 4. 60s 相同文本去重（复用上次结果）
        if not self._dedup.begin(text_hash(raw)):
            return FilterDecision("duplicate", "text_duplicate_60s", error_code="E_FILTERED_DUP")

        # 5. 业务动词白名单
        for verb in BUSINESS_VERBS:
            if verb in compact:
                self._cooldown.record_hit(session_key)
                return FilterDecision("pass", "business_verb", matched=verb)

        # 6. 宁可放行、不可误杀
        return FilterDecision("pass", "no_verb_pass_through", confidence=0.5)
