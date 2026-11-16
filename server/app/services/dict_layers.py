"""三层词表：``personal`` → ``industry`` → ``global``，个人优先。

为什么分三层
-----------
「快乐水」在老板嘴里是可乐，在隔壁店可能是雪碧。把所有人的说法混成一张表，
用两个月就会互相打架。所以：

- **personal**（员工级）：只对该员工生效，权重最高；
- **industry**（行业级，租户级）：本店/本行业通用说法；
- **global**（全局）：平台内置的常见同义词。

``dict_version`` 是**缓存击穿**用的：``sys.teach`` 之后版本号 +1，
解析缓存（结果缓存 / prompt 前缀缓存）里带旧版本号的条目自动失效——
否则「我刚教了它，它还是听不懂」这种体验会直接把用户劝退。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "DictEntry",
    "DictLayers",
    "DictVersionCache",
    "LAYERS",
    "resolve_entry",
]

#: 层优先级（**顺序即优先级**：越靠前越优先）
LAYERS: tuple[str, ...] = ("personal", "industry", "global")


@dataclass(frozen=True)
class DictEntry:
    """一条词表记录（三层共用一个结构）。"""

    layer: str
    raw_text: str
    mapped_type: str
    mapped_id: str | None = None
    version: int = 0

    @property
    def priority(self) -> int:
        """层优先级数值（越小越优先；未知层排最后）。"""
        try:
            return LAYERS.index(self.layer)
        except ValueError:
            return len(LAYERS)


@dataclass(frozen=True)
class DictResolution:
    """解析结果：命中的说法 + 是否发生过跨层覆盖。"""

    raw_text: str
    entry: DictEntry | None
    shadowed: tuple[DictEntry, ...] = ()

    @property
    def resolved(self) -> bool:
        """是否命中。"""
        return self.entry is not None

    @property
    def overridden(self) -> bool:
        """是否「个人说法覆盖了更通用的说法」（回显时可提示「按你的习惯」）。"""
        return bool(self.shadowed)

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "raw_text": self.raw_text,
            "layer": None if self.entry is None else self.entry.layer,
            "mapped_type": None if self.entry is None else self.entry.mapped_type,
            "mapped_id": None if self.entry is None else self.entry.mapped_id,
            "resolved": self.resolved,
            "overridden": self.overridden,
            "shadowed_layers": [entry.layer for entry in self.shadowed],
        }


def resolve_entry(
    raw_text: str, entries: Iterable[DictEntry | Mapping[str, Any]]
) -> DictResolution:
    """按层优先级解析一个说法（纯函数，可离线回归）。

    Args:
        raw_text: 用户原话里的片段。
        entries: 候选词表条目（可来自三层混合）。

    Returns:
        :class:`DictResolution`；多层都命中时取高层，其余记为 ``shadowed``。
    """
    target = (raw_text or "").strip()
    if not target:
        return DictResolution(raw_text=raw_text, entry=None)

    matched: list[DictEntry] = []
    for item in entries:
        entry = item if isinstance(item, DictEntry) else _to_entry(item)
        if entry is None:
            continue
        if entry.raw_text.strip() == target:
            matched.append(entry)

    if not matched:
        return DictResolution(raw_text=raw_text, entry=None)

    matched.sort(key=lambda entry: entry.priority)
    return DictResolution(
        raw_text=raw_text, entry=matched[0], shadowed=tuple(matched[1:])
    )


def _to_entry(item: Mapping[str, Any]) -> DictEntry | None:
    """把 dict 行转成 :class:`DictEntry`（缺字段返回 ``None``，不猜）。"""
    raw_text = str(item.get("raw_text") or "").strip()
    layer = str(item.get("layer") or "personal")
    mapped_type = str(item.get("mapped_type") or "")
    if not raw_text or not mapped_type:
        return None
    return DictEntry(
        layer=layer,
        raw_text=raw_text,
        mapped_type=mapped_type,
        mapped_id=(
            None if item.get("mapped_id") is None else str(item.get("mapped_id"))
        ),
        version=int(item.get("version") or 0),
    )


class DictLayers:
    """三层词表门面（`sys.teach` 与解析热路径共用）。"""

    def __init__(self, session: Any = None) -> None:
        """
        Args:
            session: 可选会话；为 ``None`` 时只用内存条目（离线/测试）。
        """
        self._session = session
        self._memory: dict[str, list[DictEntry]] = {}

    # ------------------------------------------------------------------ 写入
    def teach(
        self,
        *,
        member_id: str,
        raw_text: str,
        mapped_type: str,
        mapped_id: str | None = None,
        layer: str = "personal",
    ) -> DictEntry:
        """教一个新说法（**即时生效**：写内存 + 返回条目让调用方击穿缓存）。"""
        entry = DictEntry(
            layer=layer,
            raw_text=raw_text.strip(),
            mapped_type=mapped_type,
            mapped_id=mapped_id,
        )
        self._memory.setdefault(entry.raw_text, []).append(entry)
        return entry

    def entries_of(self, member_id: str) -> tuple[DictEntry, ...]:
        """某员工的全部说法（个人层优先排序）。"""
        del member_id  # 当前内存实现不分员工；DB 版按 member_id 过滤
        flat: list[DictEntry] = []
        for rows in self._memory.values():
            flat.extend(rows)
        return tuple(sorted(flat, key=lambda entry: entry.priority))

    # ------------------------------------------------------------------ 解析
    def resolve(self, raw_text: str) -> DictResolution:
        """按三层优先级解析。"""
        return resolve_entry(raw_text, self.entries_of(""))


class DictVersionCache:
    """``dict_version`` 缓存（教一次 → 版本 +1 → 旧缓存自然失效）。

    进程内实现 + ``threading.Lock``：云托管不部署有状态服务（D7），
    多实例之间靠「解析缓存的 key 里带版本号」保证语义正确，
    不依赖跨实例强一致。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}

    @staticmethod
    def key_of(tenant_id: str, member_id: str) -> str:
        """版本号 key（租户 + 成员）。"""
        return f"{tenant_id}:{member_id}"

    def get(self, tenant_id: str, member_id: str) -> int:
        """读版本号（没教过就是 0）。"""
        with self._lock:
            return self._versions.get(self.key_of(tenant_id, member_id), 0)

    def bump(self, tenant_id: str, member_id: str) -> int:
        """版本号 +1（``sys.teach`` / 纠错回流后必须调）。"""
        key = self.key_of(tenant_id, member_id)
        with self._lock:
            self._versions[key] = self._versions.get(key, 0) + 1
            return self._versions[key]

    def reset(self, tenant_id: str, member_id: str) -> None:
        """清零（测试/运维用）。"""
        with self._lock:
            self._versions.pop(self.key_of(tenant_id, member_id), None)

    def snapshot(self) -> dict[str, int]:
        """当前全部版本号（运维排查用）。"""
        with self._lock:
            return dict(self._versions)
