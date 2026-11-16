"""个人词表 / 商品别名解析（G3 的 ``P`` 槽位来源）。

为什么单独一层
--------------
「可乐」「快乐水」「大瓶可乐」可能都指向同一商品；反过来「可乐」在同租户里
可能同时命中「可口可乐 500ml」和「百事可乐 500ml」两种档案。
两件事必须分开处理：

1. **命中唯一** → 直接给 ``P``，``source=alias``（命中个人词表时）；
2. **命中多个** → **绝不猜**，返回候选集，交由 G5 追问（PRD 2.6 #5）。

本模块只做「文本 → 商品档案」的**确定性**映射，不碰数字、不碰金额、不调 LLM。
词表全部由调用方注入（T04 从 ``product`` / ``product_alias`` 表读出），
因此本层在测试里可以完全离线。

最长匹配优先
------------
「大瓶可乐」必须先命中「大瓶可乐」，不能再被「可乐」抢走。实现方式是
按词长降序贪心扫描 + 非重叠占位，等价于最大匹配分词。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

__all__ = [
    "AliasHit",
    "AliasResolver",
    "ProductEntry",
    "ProductLexicon",
]


@dataclass(frozen=True)
class ProductEntry:
    """一个商品档案在语音层的投影（只含解析需要的字段）。"""

    product_id: str
    name: str
    #: 商品基本单位（``U`` 的缺省值来源，见 ``slot_filler.DEFAULT_RULES``）
    base_unit: str = "件"
    #: 规格描述（卡片展示「可乐 500ml」用）
    spec: str = ""
    #: 别名 / 个人词表（命中时 ``source=alias``）
    aliases: tuple[str, ...] = ()
    #: 最近进价 / 售价（整数分，``UP`` 的 default 来源）
    last_purchase_price_fen: int | None = None
    last_sale_price_fen: int | None = None
    #: 单位换算系数：``{"箱": 24}`` 表示 1 箱 = 24 个 base_unit
    ratios: Mapping[str, float] = field(default_factory=dict)
    #: 拼音首字母 / 常见同音错写（ASR 归一后仍可能残留）
    fuzzy_terms: tuple[str, ...] = ()

    def all_terms(self) -> tuple[str, ...]:
        """正名 + 别名 + 模糊词的并集（去重保序，已去空白）。"""
        terms: dict[str, None] = {}
        for term in (self.name, *self.aliases, *self.fuzzy_terms):
            cleaned = (term or "").strip()
            if cleaned:
                terms.setdefault(cleaned, None)
        return tuple(terms)

    def alias_terms(self) -> frozenset[str]:
        """仅别名集合（用于判定 ``source=alias``）。"""
        return frozenset(term.strip() for term in self.aliases if term and term.strip())

    def display_name(self) -> str:
        """展示名：「可乐 500ml」。"""
        return f"{self.name} {self.spec}".strip()


@dataclass(frozen=True)
class AliasHit:
    """一次商品命中（可能唯一，也可能歧义）。"""

    text: str
    start: int
    end: int
    matched_term: str
    product: ProductEntry | None = None
    candidates: tuple[ProductEntry, ...] = ()
    #: 是否命中别名 / 个人词表（决定 ``source`` 是 ``asr`` 还是 ``alias``）
    is_alias: bool = False
    confidence: float = 0.0

    @property
    def resolved(self) -> bool:
        """是否唯一命中（可以填 ``P``）。"""
        return self.product is not None and not self.candidates

    @property
    def ambiguous(self) -> bool:
        """是否命中多个档案（必须澄清，禁止猜）。"""
        return len(self.candidates) > 1

    def source(self) -> str:
        """``P`` 槽位的来源标注（5 枚举之一）。"""
        return "alias" if self.is_alias else "asr"


class ProductLexicon:
    """词表索引：``词 -> 候选商品``，并记录哪些词来自别名。"""

    def __init__(self, entries: Iterable[ProductEntry] = ()) -> None:
        self._entries: dict[str, ProductEntry] = {}
        self._index: dict[str, list[ProductEntry]] = {}
        self._alias_terms: frozenset[str] = frozenset()
        self._max_term_len = 1
        for entry in entries:
            self.add(entry)

    # ------------------------------------------------------------------ 构建
    def add(self, entry: ProductEntry) -> None:
        """登记一个商品档案（同 ``product_id`` 覆盖旧的）。"""
        if entry.product_id in self._entries:
            self.remove(entry.product_id)
        self._entries[entry.product_id] = entry
        alias_terms = entry.alias_terms()
        for term in entry.all_terms():
            self._index.setdefault(term, []).append(entry)
            self._max_term_len = max(self._max_term_len, len(term))
        self._alias_terms = self._alias_terms | alias_terms

    def remove(self, product_id: str) -> None:
        """下架一个商品（停用后不得再被语音命中）。"""
        entry = self._entries.pop(product_id, None)
        if entry is None:
            return
        for term in entry.all_terms():
            bucket = self._index.get(term)
            if bucket is None:
                continue
            remaining = [item for item in bucket if item.product_id != product_id]
            if remaining:
                self._index[term] = remaining
            else:
                del self._index[term]

    # ------------------------------------------------------------------ 查询
    @property
    def entries(self) -> tuple[ProductEntry, ...]:
        """全部商品档案。"""
        return tuple(self._entries.values())

    @property
    def terms(self) -> tuple[str, ...]:
        """全部可命中词（按词长降序，最长匹配优先）。"""
        return tuple(sorted(self._index, key=len, reverse=True))

    def __len__(self) -> int:
        return len(self._entries)

    def is_alias_term(self, term: str) -> bool:
        """该词是否来自别名（决定 ``source=alias``）。"""
        return (term or "").strip() in self._alias_terms

    def lookup(self, term: str) -> AliasHit | None:
        """整词精确查询（不做子串扫描）。"""
        cleaned = (term or "").strip()
        bucket = self._index.get(cleaned)
        if not bucket:
            return None
        candidates = tuple(bucket)
        if len(candidates) == 1:
            return AliasHit(
                text=cleaned,
                start=0,
                end=len(cleaned),
                matched_term=cleaned,
                product=candidates[0],
                candidates=candidates,
                is_alias=self.is_alias_term(cleaned),
                confidence=0.95,
            )
        return AliasHit(
            text=cleaned,
            start=0,
            end=len(cleaned),
            matched_term=cleaned,
            product=None,
            candidates=candidates,
            is_alias=self.is_alias_term(cleaned),
            confidence=0.4,
        )

    def suggest(self, prefix: str, *, limit: int = 3) -> tuple[ProductEntry, ...]:
        """按前缀给候选（澄清选项用；一次 2~3 个，见 G5）。"""
        cleaned = (prefix or "").strip()
        if not cleaned:
            return ()
        seen: dict[str, ProductEntry] = {}
        for term, bucket in self._index.items():
            if not (term.startswith(cleaned) or cleaned in term):
                continue
            for entry in bucket:
                seen.setdefault(entry.product_id, entry)
            if len(seen) >= max(1, limit):
                break
        return tuple(list(seen.values())[: max(1, limit)])

    def scan(self, text: str) -> tuple[AliasHit, ...]:
        """最长匹配扫描（非重叠，按出现位置升序）。

        Args:
            text: 已归一化的原话。

        Returns:
            命中的 :class:`AliasHit` 序列；同一商品只保留最长的一次命中。
        """
        if not text:
            return ()
        hits: list[AliasHit] = []
        cursor = 0
        while cursor < len(text):
            matched = self._match_at(text, cursor)
            if matched is None:
                cursor += 1
                continue
            hits.append(matched)
            cursor = matched.end
        return tuple(hits)

    def _match_at(self, text: str, start: int) -> AliasHit | None:
        """从 ``start`` 开始尝试最长匹配。"""
        upper = min(len(text), start + self._max_term_len)
        for end in range(upper, start, -1):
            window = text[start:end]
            bucket = self._index.get(window)
            if not bucket:
                continue
            candidates = tuple(bucket)
            is_alias = self.is_alias_term(window)
            if len(candidates) == 1:
                return AliasHit(
                    text=window,
                    start=start,
                    end=end,
                    matched_term=window,
                    product=candidates[0],
                    candidates=candidates,
                    is_alias=is_alias,
                    confidence=0.95,
                )
            return AliasHit(
                text=window,
                start=start,
                end=end,
                matched_term=window,
                product=None,
                candidates=candidates,
                is_alias=is_alias,
                confidence=0.4,
            )
        return None


class AliasResolver:
    """``P`` 槽位解析器：词表 + 个人词表覆盖。"""

    def __init__(
        self,
        entries: Iterable[ProductEntry] = (),
        *,
        personal_aliases: Mapping[str, str] | None = None,
    ) -> None:
        """
        Args:
            entries: 商品档案。
            personal_aliases: 个人词表 ``{说法: product_id}``（教说法回流，D6 审核后生效）。
        """
        self._lexicon = ProductLexicon(entries)
        #: ``{说法: product_id}``；个人词表命中一律 ``source=alias``
        self._personal: dict[str, str] = {
            (term or "").strip(): product_id
            for term, product_id in (personal_aliases or {}).items()
            if (term or "").strip()
        }

    # ------------------------------------------------------------------ 属性
    @property
    def lexicon(self) -> ProductLexicon:
        """底层词表（供 prompt_builder 取 TopN）。"""
        return self._lexicon

    @property
    def personal_aliases(self) -> Mapping[str, str]:
        """个人词表快照。"""
        return dict(self._personal)

    def learn_alias(self, term: str, product_id: str) -> None:
        """教说法（``product.alias.add`` / ``sys.teach`` 落地后调用）。"""
        cleaned = (term or "").strip()
        if cleaned:
            self._personal[cleaned] = product_id

    # ------------------------------------------------------------------ 解析
    def resolve(self, text: str) -> AliasHit | None:
        """整串视为一个商品词（澄清点选回填时用）。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        target = self._personal.get(cleaned)
        if target is not None:
            entry = self._lexicon.lookup(target)
            if entry is not None and entry.product is not None:
                return AliasHit(
                    text=cleaned,
                    start=0,
                    end=len(cleaned),
                    matched_term=cleaned,
                    product=entry.product,
                    candidates=(entry.product,),
                    is_alias=True,
                    confidence=0.97,
                )
        hit = self._lexicon.lookup(cleaned)
        if hit is not None:
            return hit
        for term, product_id in self._personal.items():
            if term in cleaned:
                entry = self._lexicon.lookup(product_id)
                if entry is not None and entry.product is not None:
                    return AliasHit(
                        text=cleaned,
                        start=cleaned.index(term),
                        end=cleaned.index(term) + len(term),
                        matched_term=term,
                        product=entry.product,
                        candidates=(entry.product,),
                        is_alias=True,
                        confidence=0.9,
                    )
        return None

    def scan(self, text: str) -> tuple[AliasHit, ...]:
        """从整句里扫出商品（个人词表优先，其次最长匹配词表）。"""
        if not text:
            return ()
        hits: list[AliasHit] = []
        for term, product_id in sorted(self._personal.items(), key=lambda item: len(item[0]), reverse=True):
            start = text.find(term)
            if start < 0:
                continue
            entry = self._lexicon.lookup(product_id)
            if entry is None or entry.product is None:
                continue
            hits.append(
                AliasHit(
                    text=term,
                    start=start,
                    end=start + len(term),
                    matched_term=term,
                    product=entry.product,
                    candidates=(entry.product,),
                    is_alias=True,
                    confidence=0.97,
                )
            )
        for hit in self._lexicon.scan(text):
            if any(existing.start < hit.end and hit.start < existing.end for existing in hits):
                continue
            hits.append(hit)
        return tuple(sorted(hits, key=lambda item: item.start))

    def options_for(self, hit: AliasHit, *, limit: int = 3) -> tuple[str, ...]:
        """歧义命中时的可点选项（2~3 个，G5 硬约束）。"""
        pool = hit.candidates or self._lexicon.entries
        names: dict[str, None] = {}
        for entry in pool:
            names.setdefault(entry.display_name(), None)
            if len(names) >= max(2, limit):
                break
        return tuple(names)
