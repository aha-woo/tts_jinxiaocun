"""道具定义投影：**``goods.json`` 是唯一真相源**，服务端不复制价格常量。

为什么要一张平台级表
------------------
发货回调是**无用户上下文**的推送（只有 ``OutTradeNo`` + ``GoodsId``），
它不能去读小程序前端，也不该去读某个租户的配置。所以道具定义要有一份
平台级（无 ``tenant_id``）的只读投影，种子从 ``goods.json`` 灌入。

**价格绝不硬编码**：这里所有字段都取自契约 JSON；
``tests/test_entitlement_separation.py`` 会断言「代码里没有出现价格字面量」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ContractBundle, get_contracts
from app.models.pay import GoodsItem

__all__ = [
    "GoodsCatalog",
    "GoodsView",
    "seed_goods",
]

#: 上架状态（与 DB CHECK 一致）
STATUS_ON_SHELF = "on_shelf"
STATUS_OFF_SHELF = "off_shelf"


def _format_price_text(fen: int) -> str:
    """分 -> 人民币展示文案（整数元省去小数，有角分保留两位）。"""
    yuan, rem = divmod(max(0, fen), 100)
    return f"¥{yuan}" if rem == 0 else f"¥{fen / 100:.2f}"


def _build_goods_desc(view: "GoodsView") -> str:
    """拼装套餐/加油包摘要说明，供前端购买卡片展示。"""
    parts: list[str] = []
    if view.quota_amount > 0:
        parts.append(f"{view.quota_amount} 次额度")
    if view.validity_days > 0:
        parts.append(f"{view.validity_days} 天有效")
    head = " · ".join(parts)
    if head and view.benefits_zh:
        return f"{head}｜{view.benefits_zh}"
    return view.benefits_zh or head


@dataclass(frozen=True)
class GoodsView:
    """一个道具的视图（字段与 ``goods.json`` 一一对应）。"""

    goods_id: str
    goods_type: str
    name_zh: str
    price_fen: int
    quota_amount: int
    validity_days: int
    status: str
    plan_code: str
    benefits_zh: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "goods_id": self.goods_id,
            "goods_type": self.goods_type,
            "name_zh": self.name_zh,
            "name": self.name_zh,
            "price_fen": self.price_fen,
            "price_text": _format_price_text(self.price_fen),
            "quota_amount": self.quota_amount,
            "validity_days": self.validity_days,
            "status": self.status,
            "plan_code": self.plan_code,
            "benefits_zh": self.benefits_zh,
            "desc": _build_goods_desc(self),
            "auto_renew": False,
        }


class GoodsCatalog:
    """道具目录（读契约，不读数据库 → 下单时永远与前端展示一致）。"""

    def __init__(self, contracts: ContractBundle | None = None) -> None:
        """
        Args:
            contracts: 共享契约（缺省取进程内单例）。
        """
        self._contracts = contracts or get_contracts()

    def all(self, *, on_shelf_only: bool = False) -> tuple[GoodsView, ...]:
        """全部道具。"""
        views: list[GoodsView] = []
        for goods in self._contracts.goods.get("goods", []):
            view = _view_of(goods)
            if on_shelf_only and view.status != STATUS_ON_SHELF:
                continue
            views.append(view)
        return tuple(views)

    def get(self, goods_id: str) -> GoodsView | None:
        """按 id 取道具（**不存在就返回 None，绝不猜价格**）。"""
        for view in self.all():
            if view.goods_id == goods_id:
                return view
        return None

    def require(self, goods_id: str) -> GoodsView:
        """按 id 取道具，不存在直接报错。

        Raises:
            ValueError: 道具不存在或已下架。
        """
        view = self.get(goods_id)
        if view is None:
            raise ValueError(f"道具 {goods_id!r} 不在 goods.json 中")
        if view.status != STATUS_ON_SHELF:
            raise ValueError(f"道具 {goods_id!r} 已下架（status={view.status}）")
        return view

    def plan_codes(self) -> tuple[str, ...]:
        """契约声明的权益档位。"""
        return tuple(str(code) for code in self._contracts.goods.get("plan_codes", []))

    def min_payment_fen(self) -> int:
        """最低支付金额（分）——虚拟支付平台约束。"""
        constraints = dict(self._contracts.goods.get("constraints") or {})
        return int(constraints.get("min_payment_fen") or 100)

    def channel(self) -> str:
        """支付通道（**只能是** ``wx.requestVirtualPayment``）。"""
        constraints = dict(self._contracts.goods.get("constraints") or {})
        return str(constraints.get("payment_channel") or "wx.requestVirtualPayment")

    def forbidden_channels(self) -> tuple[str, ...]:
        """明令禁止的通道（普通商户号 / 外部链接 / 客服转账）。"""
        constraints = dict(self._contracts.goods.get("constraints") or {})
        return tuple(constraints.get("forbidden_channels") or ())


def seed_goods(session: Session, contracts: ContractBundle | None = None) -> int:
    """把 ``goods.json`` 投影进 ``goods_item`` 表（幂等 upsert）。

    Args:
        session: 会话（平台级表，无租户上下文要求）。
        contracts: 共享契约。

    Returns:
        写入/更新的行数。
    """
    catalog = GoodsCatalog(contracts)
    written = 0
    for view in catalog.all():
        row = session.execute(
            select(GoodsItem).where(GoodsItem.goods_id == view.goods_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            session.add(
                GoodsItem(
                    goods_id=view.goods_id,
                    goods_name=view.name_zh,
                    goods_type=view.goods_type,
                    price_fen=view.price_fen,
                    quota_amount=view.quota_amount,
                    validity_days=view.validity_days,
                    status=view.status,
                )
            )
        else:
            row.goods_name = view.name_zh
            row.goods_type = view.goods_type
            row.price_fen = view.price_fen
            row.quota_amount = view.quota_amount
            row.validity_days = view.validity_days
            row.status = view.status
        written += 1
    session.flush()
    return written


def _view_of(goods: Mapping[str, Any]) -> GoodsView:
    """契约 dict → 视图。"""
    return GoodsView(
        goods_id=str(goods["goods_id"]),
        goods_type=str(goods["goods_type"]),
        name_zh=str(goods["name_zh"]),
        price_fen=int(goods["price_fen"]),
        quota_amount=int(goods.get("quota_amount") or 0),
        validity_days=int(goods.get("validity_days") or 0),
        status=str(goods.get("status") or STATUS_ON_SHELF),
        plan_code=str(goods.get("plan_code") or "free"),
        benefits_zh=str(goods.get("benefits_zh") or ""),
    )


def new_out_trade_no(tenant_id: uuid.UUID, *, stamp: str) -> str:
    """生成 ``out_trade_no``（非幂等场景）：租户前缀 + 时间戳 + 随机段。

    长度 ≤64（DB ``String(64)``），字母数字与下划线，避免部分渠道的字符限制。
    """
    prefix = str(tenant_id).replace("-", "")[:8]
    random_part = uuid.uuid4().hex[:8]
    return f"VIL{prefix}{stamp}{random_part}"[:64]


def out_trade_no_of(tenant_id: uuid.UUID, idempotency_key: str) -> str:
    """由**幂等键派生** ``out_trade_no``：同一个键必然算出同一个单号。

    这就是「下单幂等」的全部实现：不额外建幂等表，也不靠内存缓存——
    同一个 ``Idempotency-Key`` 再来一次，算出来的单号一样，
    ``UNIQUE(tenant_id, out_trade_no)`` 自然把它拦成同一单。
    """
    import hashlib

    digest = hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:26]
    return f"VIL{digest}"


def goods_ids(views: Sequence[GoodsView]) -> tuple[str, ...]:
    """道具 id 列表（测试断言「价格来自契约」时用）。"""
    return tuple(view.goods_id for view in views)
