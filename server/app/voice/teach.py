"""``sys.teach`` 的槽位补全：把「教一句话」拆成（说法, 指向对象）两半。

为什么需要这一层
----------------
契约里 ``sys.teach`` 只有 ``ALIAS`` 一个槽位，**没有指向对象**；而落库的
个人词表必须有 ``mapped_type`` + ``mapped_id``。于是「以后我说快乐水就是可乐」
这句话落到槽位上只剩一坨文本：

- 不拆 → 写入器只能报错「要指向哪个商品」→ **卡片建得出来、确认必失败**；
- 瞎拆 → 把「快乐水就是可乐」整串当成说法，教进去的说法与商品都对不上。

所以这里只做**确定性切分**，不猜：

============================  ==============  ==============
原话                          说法（alias）    指向（target）
============================  ==============  ==============
``可乐别名叫「快乐水」``        快乐水           可乐
``以后我说「拿一条」就是烟``    拿一条           烟
``记住快乐水就是可乐``          快乐水           可乐
``以后「大瓶可乐」就指 2 升``   大瓶可乐         2 升
============================  ==============  ==============

切不出指向对象就返回 ``reason="no_target"``，由路由层去追问 —— 绝不瞎配。

路由层拿到 :class:`TeachPlan` 之后只有两种处理（见 ``api/v1/voice.py``）：

- ``reason="ok"``：把这句意图**改写成 ``product.alias.add``**（它才有 ``P`` +
  ``ALIAS`` 两个必填槽位），走正常卡片 → 确认 → 写真别名，教完立刻生效；
- 其余：走澄清（2~3 个可点候选），**不建卡**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "TeachPlan",
    "TeachSplit",
    "TeachTarget",
    "plan_teach",
    "split_teach",
]


class _Resolvable(Protocol):
    """本模块只用到解析器的两个方法（便于单测传替身）。"""

    def resolve(self, text: str) -> Any: ...

    def scan(self, text: str) -> tuple[Any, ...]: ...


#: 引号（教说法时用户常给说法加引号，切分前先剥掉）
_QUOTES = "「」『』\"'‘’《》"
#: 口语句首废话（切分后从「说法」一侧剥掉）
_LEAD_WORDS = (
    "以后我说",
    "以后说",
    "以后你要是听我说",
    "以后",
    "请记住",
    "记住",
    "记一下",
    "记着",
    "记下",
    "帮我",
)
#: 口语句尾废话
_TAIL_WORDS = ("那种", "这种", "这个", "那个")
#: 线索词在**中间**：左=指向、右=说法（方向与下面相反，必须先判）
_MIDDLE_CUES = ("别名叫", "别名是", "外号叫", "又叫做", "又叫", "简称")
#: 线索词在**中间**：左=说法、右=指向
_SPLIT_CUES = ("指的就是", "指的是", "就是说", "就是", "就指", "等于", "说成", "叫做", "叫", "＝", "=")
#: 只切第一个出现的线索词，所以长线索要排在短线索前面（``叫做`` 先于 ``叫``）
_PUNCTUATION = " 　，,。.、：:；;！!？?"


def _clean(text: str) -> str:
    """剥引号、句首句尾口语词与标点（**只做格式清洗，不改写词义**）。"""
    value = (text or "").strip()
    for quote in _QUOTES:
        value = value.replace(quote, "")
    value = value.strip(_PUNCTUATION)
    for word in _LEAD_WORDS:
        if value.startswith(word):
            value = value[len(word) :].strip(_PUNCTUATION)
            break
    for word in _TAIL_WORDS:
        if value.endswith(word):
            value = value[: -len(word)].strip(_PUNCTUATION)
            break
    return value.strip(_PUNCTUATION)


@dataclass(frozen=True)
class TeachSplit:
    """切分结果（纯文本层，不碰数据库）。"""

    alias: str | None
    target: str | None
    cue: str | None = None


def split_teach(text: str) -> TeachSplit:
    """把一句「教说法」切成（说法, 指向）。

    Args:
        text: 用户原话（ASR 文本）。

    Returns:
        :class:`TeachSplit`；切不出来的一侧是 ``None``。

    Examples:
        >>> split_teach("可乐别名叫「快乐水」")
        TeachSplit(alias='快乐水', target='可乐', cue='别名叫')
        >>> split_teach("记住快乐水就是可乐")
        TeachSplit(alias='快乐水', target='可乐', cue='就是')
    """
    raw = (text or "").strip()
    if not raw:
        return TeachSplit(None, None, None)
    for cue in _MIDDLE_CUES:
        position = raw.find(cue)
        if position > 0:
            return TeachSplit(
                alias=_clean(raw[position + len(cue) :]) or None,
                target=_clean(raw[:position]) or None,
                cue=cue,
            )
    for cue in _SPLIT_CUES:
        position = raw.find(cue)
        if position > 0:
            return TeachSplit(
                alias=_clean(raw[:position]) or None,
                target=_clean(raw[position + len(cue) :]) or None,
                cue=cue,
            )
    return TeachSplit(alias=_clean(raw) or None, target=None, cue=None)


@dataclass(frozen=True)
class TeachTarget:
    """已落到真实商品上的指向对象。"""

    product_id: str
    product_name: str
    #: 目标是靠别名命中的（``source`` 要写 ``alias``，不是 ``asr``）
    from_alias: bool = False


@dataclass(frozen=True)
class TeachPlan:
    """路由层的决策输入。

    ``reason`` 取值：``ok`` / ``no_alias`` / ``no_target`` /
    ``unknown_target`` / ``ambiguous``。
    """

    reason: str
    alias: str | None = None
    target_text: str | None = None
    target: TeachTarget | None = None
    #: 澄清时的可点候选（2~3 个展示名）
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """是否可以直接落成别名。"""
        return self.reason == "ok" and self.target is not None


def _catalog_names(resolver: _Resolvable, limit: int) -> tuple[str, ...]:
    """词表里的前 N 个商品名（澄清候选兜底，**不是**按热度猜）。"""
    lexicon = getattr(resolver, "lexicon", None)
    #: ``ProductLexicon.entries`` 是 **property**（返回元组），但也允许传方法形态的替身
    entries = getattr(lexicon, "entries", ()) if lexicon is not None else ()
    if callable(entries):
        entries = entries()
    names: list[str] = []
    for entry in entries:
        name = str(getattr(entry, "name", "") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return tuple(names)


def _resolve(resolver: _Resolvable, text: str) -> Any:
    """整串解析优先；整串不中再扫一遍（``2 升`` 这类带单位的说法）。"""
    hit = resolver.resolve(text)
    if hit is not None:
        return hit
    hits = resolver.scan(text)
    return hits[0] if hits else None


def plan_teach(
    *,
    text: str,
    resolver: _Resolvable,
    limit: int = 3,
) -> TeachPlan:
    """切分 + 解析，产出路由层要的决策（**不写库、不建卡**）。

    Args:
        text: 用户原话。
        resolver: 商品解析器（``runtime.products``）。
        limit: 澄清候选上限（契约硬约束 2~3）。

    Returns:
        :class:`TeachPlan`。
    """
    split = split_teach(text)
    if not split.alias:
        return TeachPlan(reason="no_alias", candidates=_catalog_names(resolver, limit))
    if not split.target:
        return TeachPlan(
            reason="no_target",
            alias=split.alias,
            candidates=_catalog_names(resolver, limit),
        )

    hit = _resolve(resolver, split.target)
    if hit is None:
        return TeachPlan(
            reason="unknown_target",
            alias=split.alias,
            target_text=split.target,
            candidates=_catalog_names(resolver, limit),
        )
    if getattr(hit, "ambiguous", False) or getattr(hit, "product", None) is None:
        options = ()
        options_for = getattr(resolver, "options_for", None)
        if callable(options_for):
            options = tuple(options_for(hit, limit=limit))
        return TeachPlan(
            reason="ambiguous",
            alias=split.alias,
            target_text=split.target,
            candidates=options or _catalog_names(resolver, limit),
        )

    product = hit.product
    return TeachPlan(
        reason="ok",
        alias=split.alias,
        target_text=split.target,
        target=TeachTarget(
            product_id=str(product.product_id),
            product_name=str(product.name),
            from_alias=bool(getattr(hit, "is_alias", False)),
        ),
    )


def clarify_options(candidates: tuple[str, ...], *, fallback: str, limit: int = 3) -> list[str]:
    """把候选凑成契约要求的 2~3 个可点选项（不够就补一个**明确的放弃项**）。"""
    options = [str(item).strip() for item in candidates if str(item).strip()][:limit]
    if len(options) < 2:
        options.append(fallback)
    if len(options) < 2:  # pragma: no cover - 兜底，正常走不到
        options.append("先算了")
    return options
