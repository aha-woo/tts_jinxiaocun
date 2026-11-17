"""写意图的**实体**落库：商品档案 / 别名 / 客户供应商 / 价格 / 个人词表。

为什么要单独一个模块
--------------------
:meth:`app.services.ledger.LedgerService._write_documents` 负责的是「单据 + 库存 + 应收」。
但契约里还有 8 个意图写的是**别的实体**（``side_effect.entities``）：

===============  ==============================
intent           entities
===============  ==============================
product.create   product
product.update   product
product.disable  product
product.alias.add  alias
partner.create   partner
partner.update   partner
price.update     product_price
sys.teach        dict_personal
===============  ==============================

以前这些意图全都静默落成「一张什么都没有的单据」——用户看到「已记：新建可乐 ¥0.00」，
回店里一查商品档案里根本没有可乐。这是「看到成功但账是错的」里最隐蔽的一类，
因为它连数字都自洽（金额 0）。

本模块把这条变成**白名单**：
- 列在这里的意图 → 真写实体，并返回写到了哪一行（回执能说清）；
- **没列的 side_effect 一律报错**（:class:`UnsupportedSideEffect`），
  绝不静默写单——「没实现」必须表现出来，不能假装成功。

两条实现纪律
------------
1. **幂等**：同一句话连说两遍（或卡片复活后确认两次）不得产生两条商品 /
   两条别名。命中已有实体时返回 ``created=False`` 并复用；
2. **不猜**：槽位不足以表达目标时（例如 ``sys.teach`` 只有别名、没有指向对象）
   直接 ``E_VALIDATION``，不编一个映射出来。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.errors import AppError
from app.config import ContractBundle, get_contracts
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import PersonalDict
from app.models.partner import Partner
from app.tenant.repository import tenant_repository
from app.utils.money import parse_cny_text
from app.voice.number_normalizer import parse_quantity

__all__ = [
    "ENTITY_INTENTS",
    "EntityWriteResult",
    "EntityWriter",
    "UnsupportedSideEffect",
]

#: 走「实体写入」而不是「写单据」的意图（契约 ``side_effect.entities`` 对应）。
ENTITY_INTENTS: frozenset[str] = frozenset(
    {
        "product.create",
        "product.update",
        "product.disable",
        "product.alias.add",
        "partner.create",
        "partner.update",
        "price.update",
        "sys.teach",
    }
)

#: 与钱有关的字段（TF 六值枚举里的两个）
_PRICE_FIELDS: Mapping[str, str] = {
    "进价": "last_purchase_price_fen",
    "售价": "last_sale_price_fen",
}

#: 可以「只改一个字段」的意图允许改哪些字段（其余一律明说不支持）
_PRODUCT_FIELDS: frozenset[str] = frozenset({"进价", "售价", "名称"})
_PARTNER_FIELDS: frozenset[str] = frozenset({"名称", "备注"})


class UnsupportedSideEffect(Exception):
    """``side_effect`` 声明了某个实体，但没有任何代码实现它。

    这个异常**不对外**：``LedgerService`` 会把它转成 ``E_VALIDATION``（422），
    并把 ``intent_id`` / ``entities`` 带进 data，方便排障时一眼看出是哪条没实现。
    """

    def __init__(self, intent_id: str, entities: tuple[str, ...]) -> None:
        super().__init__(f"{intent_id} 的 side_effect 未实现：entities={list(entities)}")
        self.intent_id = intent_id
        self.entities = entities


@dataclass(frozen=True)
class EntityWriteResult:
    """实体写入结果（回执里要能说清「写到了哪一行」）。"""

    intent_id: str
    entity: str
    entity_id: str
    summary: str
    created: bool

    def as_dict(self) -> dict[str, Any]:
        """JSON 形态。"""
        return {
            "intent_id": self.intent_id,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "summary": self.summary,
            "created": self.created,
        }


class EntityWriter:
    """按意图白名单把槽位落到实体表（**没有实现就报错**）。"""

    def __init__(self, session: Session, *, contracts: ContractBundle | None = None) -> None:
        """
        Args:
            session: 已绑定租户上下文的写会话。
            contracts: 共享契约（仅用于取槽位默认值，不再复制任何业务常量）。
        """
        self._session = session
        self._contracts = contracts or get_contracts()
        self._handlers: Mapping[str, Callable[..., EntityWriteResult]] = {
            "product.create": self._product_create,
            "product.update": self._product_update,
            "product.disable": self._product_disable,
            "product.alias.add": self._product_alias_add,
            "partner.create": self._partner_create,
            "partner.update": self._partner_update,
            "price.update": self._price_update,
            "sys.teach": self._sys_teach,
        }

    # ------------------------------------------------------------------ 入口
    def apply(
        self,
        *,
        intent_id: str,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str] | None = None,
    ) -> EntityWriteResult:
        """按意图分派；未实现的意图抛 :class:`UnsupportedSideEffect`。

        Args:
            intent_id: 意图 id。
            tenant_id: 租户（上下文里的，不接受入参覆盖）。
            store_id: 门店。
            member_id: 操作人（个人词表/个人别名按它隔离）。
            slots: 18 槽位的值。
            source: 槽位来源（``asr`` / ``pending`` …），仅用于报错时指出缺什么。

        Returns:
            :class:`EntityWriteResult`。

        Raises:
            UnsupportedSideEffect: 该意图没有实现。
            AppError: 槽位不足或值非法（``E_VALIDATION``）。
        """
        handler = self._handlers.get(intent_id)
        if handler is None:
            raise UnsupportedSideEffect(intent_id, tuple(sorted(ENTITY_INTENTS)))
        return handler(
            tenant_id=tenant_id,
            store_id=store_id,
            member_id=member_id,
            slots=dict(slots),
            source=dict(source or {}),
        )

    # ------------------------------------------------------------------ 商品
    def _product_create(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``product.create``：建商品档案（含可选换算系数与别名）。"""
        del store_id, source
        name = _text(slots.get("P"))
        if not name:
            raise _missing("product.create", "商品名")

        product, created = self._upsert_product(
            tenant_id=tenant_id,
            name=name,
            unit=_text(slots.get("U")) or "个",
            purchase_fen=_money_fen(slots.get("UP")),
            sale_fen=None,
        )

        extras: list[str] = []
        ratio = _number(slots.get("RATIO"))
        from_unit = _text(slots.get("U"))
        if ratio is not None and ratio > 0 and from_unit and from_unit != product.base_unit:
            # 换算会影响历史数量重算 → 与 R3 的 product.unit.set 同口径：先落 pending，
            # 由电脑版微信大屏审批，**不在语音链路里直接生效**
            self._add_conversion(
                tenant_id=tenant_id,
                product_id=product.product_id,
                from_unit=from_unit,
                to_unit=product.base_unit,
                ratio=ratio,
            )
            extras.append(f"换算 {from_unit}→{product.base_unit} ×{_number_text(ratio)}（待大屏审批后生效）")

        alias_text = _text(slots.get("ALIAS"))
        if alias_text:
            self._add_product_alias(
                tenant_id=tenant_id,
                member_id=member_id,
                product_id=product.product_id,
                alias_text=alias_text,
            )
            extras.append(f"别名「{alias_text}」")

        summary = (
            f"{'新建' if created else '商品已存在，复用'}商品「{product.name}」"
            f"（基本单位 {product.base_unit}，进价 {_fen_text(product.last_purchase_price_fen)}）"
        )
        if extras:
            summary += "；" + "；".join(extras)
        return EntityWriteResult(
            intent_id="product.create",
            entity="product",
            entity_id=str(product.product_id),
            summary=summary,
            created=created,
        )

    def _product_update(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``product.update``：只改一个字段（字段由 ``TF`` 指定）。"""
        del store_id, member_id, source
        product = self._require_product(tenant_id, _text(slots.get("P")), intent="product.update")
        field_name = _text(slots.get("TF"))
        return self._apply_product_field(
            tenant_id=tenant_id, product=product, intent_id="product.update", field_name=field_name,
            raw_value=slots.get("TV"),
        )

    def _product_disable(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``product.disable``：停用（非删除，历史单据保留）。"""
        del store_id, member_id, source
        product = self._require_product(tenant_id, _text(slots.get("P")), intent="product.disable")
        already = str(product.status) == "disabled"
        product.status = "disabled"
        self._session.flush()
        return EntityWriteResult(
            intent_id="product.disable",
            entity="product",
            entity_id=str(product.product_id),
            summary=(
                f"商品「{product.name}」原本就是停用状态（未重复操作）"
                if already
                else f"已停用商品「{product.name}」：新单据不再选到它，历史单据全部保留"
            ),
            created=not already,
        )

    def _product_alias_add(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``product.alias.add``：给商品加一个个人叫法。

        同时写两处（缺一处「教说法」就只是一半生效）：

        - ``product_alias``：**解析词表真正读的是这张表**（``load_product_lexicon``），
          不写它 → 说了等于没说，下一句照样识别不出来；
        - ``personal_dict``：``sys.teach`` 契约里的落点（``entities=[dict_personal]``）。

        两张表都是 ``member`` 维度、都有唯一约束，重复教同一句不会写两条。
        """
        del store_id, source
        product = self._require_product(tenant_id, _text(slots.get("P")), intent="product.alias.add")
        alias_text = _strip_alias_cue(_text(slots.get("ALIAS")), product_name=product.name)
        if not alias_text:
            raise _missing("product.alias.add", "别名")
        created = self._add_product_alias(
            tenant_id=tenant_id,
            member_id=member_id,
            product_id=product.product_id,
            alias_text=alias_text,
        )
        self._add_personal_dict(
            tenant_id=tenant_id,
            member_id=member_id,
            raw_text=alias_text,
            mapped_type="product",
            mapped_id=product.product_id,
        )
        return EntityWriteResult(
            intent_id="product.alias.add",
            entity="product_alias",
            entity_id=str(product.product_id),
            summary=(
                f"已记住「{alias_text}」= 商品「{product.name}」（只说这个说法，别人不受影响）"
                if created
                else f"「{alias_text}」本来就指向商品「{product.name}」（未重复添加）"
            ),
            created=created,
        )

    # ------------------------------------------------------------------ 伙伴
    def _partner_create(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``partner.create``：建客户/供应商（类型由 ``C``/``S`` 哪个有值决定）。"""
        del store_id, member_id, source
        partner_type = _partner_type(slots)
        name = _text(slots.get("TV")) or _text(slots.get("ALIAS"))
        if not name:
            name = _text(slots.get("C")) or _text(slots.get("S"))
        if not name:
            raise _missing("partner.create", "客户或供应商的名字")

        existing = self._find_partner(tenant_id, partner_type, name)
        if existing is not None:
            return EntityWriteResult(
                intent_id="partner.create",
                entity="partner",
                entity_id=str(existing.partner_id),
                summary=f"{_partner_label(partner_type)}「{existing.name}」已存在（未重复建档）",
                created=False,
            )
        partner = tenant_repository(self._session, Partner).add(
            Partner(type=partner_type, name=name, status="active")
        )
        self._session.flush()
        note = _text(slots.get("N"))
        if note:
            partner.note = note
            self._session.flush()
        return EntityWriteResult(
            intent_id="partner.create",
            entity="partner",
            entity_id=str(partner.partner_id),
            summary=f"已建{_partner_label(partner_type)}「{partner.name}」",
            created=True,
        )

    def _partner_update(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``partner.update``：改伙伴的一个字段（名称 / 备注）。"""
        del store_id, member_id, source
        partner_type = _partner_type(slots)
        target_name = _text(slots.get("C")) or _text(slots.get("S")) or _text(slots.get("TV"))
        if not target_name:
            raise _missing("partner.update", "要改的客户或供应商名字")
        partner = self._find_partner(tenant_id, partner_type, target_name)
        if partner is None:
            raise AppError(
                "E_VALIDATION",
                message=f"没有找到{_partner_label(partner_type)}「{target_name}」；请先建档",
                data={"intent_id": "partner.update", "name": target_name},
            )
        field_name = _text(slots.get("TF"))
        raw_value = slots.get("TV") if field_name != "名称" else _text(slots.get("N")) or slots.get("TV")
        if field_name not in _PARTNER_FIELDS:
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"伙伴档案没有「{field_name or '未指定'}」这个字段；"
                    "电话请在管理页加密录入，语音链路不接收明文手机号"
                ),
                data={"intent_id": "partner.update", "target_field": field_name},
            )
        if field_name == "名称":
            new_name = _text(raw_value) or _text(slots.get("TV"))
            if not new_name:
                raise _missing("partner.update", "新名字")
            clash = self._find_partner(tenant_id, partner_type, new_name)
            if clash is not None and clash.partner_id != partner.partner_id:
                raise AppError(
                    "E_VALIDATION",
                    message=f"已经有一个{_partner_label(partner_type)}叫「{new_name}」了；合并请走大屏的合并功能",
                    data={"intent_id": "partner.update"},
                )
            old_name = str(partner.name)
            partner.name = new_name
            self._session.flush()
            return EntityWriteResult(
                intent_id="partner.update",
                entity="partner",
                entity_id=str(partner.partner_id),
                summary=f"{_partner_label(partner_type)}「{old_name}」改名为「{new_name}」",
                created=False,
            )
        note = _text(raw_value)
        partner.note = note or None
        self._session.flush()
        return EntityWriteResult(
            intent_id="partner.update",
            entity="partner",
            entity_id=str(partner.partner_id),
            summary=f"{_partner_label(partner_type)}「{partner.name}」的备注已更新",
            created=False,
        )

    # ------------------------------------------------------------------ 价格
    def _price_update(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``price.update``：改进价/售价（**历史单据不回溯**，只影响之后的默认价）。"""
        del store_id, member_id, source
        product = self._require_product(tenant_id, _text(slots.get("P")), intent="price.update")
        field_name = _text(slots.get("TF"))
        if field_name not in _PRICE_FIELDS:
            raise AppError(
                "E_VALIDATION",
                message=f"改价只能改进价或售价，收到「{field_name or '未指定'}」",
                data={"intent_id": "price.update", "target_field": field_name},
            )
        result = self._apply_product_field(
            tenant_id=tenant_id, product=product, intent_id="price.update", field_name=field_name,
            raw_value=slots.get("TV"),
        )
        effective = _text(slots.get("EF"))
        if effective:
            result = EntityWriteResult(
                intent_id=result.intent_id,
                entity=result.entity,
                entity_id=result.entity_id,
                summary=f"{result.summary}（从 {effective} 起生效；历史单据价格不回溯）",
                created=result.created,
            )
        return result

    # ------------------------------------------------------------------ 词表
    def _sys_teach(
        self,
        *,
        tenant_id: uuid.UUID,
        store_id: uuid.UUID,
        member_id: uuid.UUID,
        slots: Mapping[str, Any],
        source: Mapping[str, str],
    ) -> EntityWriteResult:
        """``sys.teach``：教一个新说法。

        契约里 ``sys.teach`` 只有 ``ALIAS`` 一个槽位——**没有指向对象**。
        所以「记住快乐水就是可乐」这句话，落到槽位上只剩「快乐水」，
        而 ``personal_dict`` 必须写 ``mapped_type`` + ``mapped_id``（约束
        ``mapped_type IN ('product','partner','number','unit','intent')``）。

        这里**不猜**：拿不到指向对象就明确报错，让用户补一句；
        绝不写一条 ``mapped_type='product'`` / ``mapped_id=NULL`` 的空映射
        （那会让解析层以为「快乐水」解析成功了，实际指向空气）。
        """
        del store_id, source
        alias_text = _text(slots.get("ALIAS"))
        if not alias_text:
            raise _missing("sys.teach", "要记住的说法")
        product = self._find_product_by_name(tenant_id, alias_text)
        if product is None:
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"「{alias_text}」要指向哪个商品/客户？请说完整一点："
                    "「记住快乐水就是可乐」——这样我才能把说法和档案连起来，不会瞎猜"
                ),
                data={"intent_id": "sys.teach", "alias": alias_text},
            )
        created = self._add_personal_dict(
            tenant_id=tenant_id,
            member_id=member_id,
            raw_text=alias_text,
            mapped_type="product",
            mapped_id=product.product_id,
        )
        return EntityWriteResult(
            intent_id="sys.teach",
            entity="personal_dict",
            entity_id=str(product.product_id),
            summary=(
                f"已记住「{alias_text}」= 商品「{product.name}」"
                if created
                else f"「{alias_text}」本来就指向「{product.name}」（未重复记录）"
            ),
            created=created,
        )

    # ------------------------------------------------------------------ 内部
    def _apply_product_field(
        self,
        *,
        tenant_id: uuid.UUID,
        product: Product,
        intent_id: str,
        field_name: str,
        raw_value: Any,
    ) -> EntityWriteResult:
        """按 ``TF`` 改商品的一个字段（价格 / 名称），其余字段明确拒绝。"""
        if field_name not in _PRODUCT_FIELDS:
            raise AppError(
                "E_VALIDATION",
                message=(
                    f"商品档案没有「{field_name or '未指定'}」这个字段；"
                    "能改的只有进价 / 售价 / 名称"
                ),
                data={"intent_id": intent_id, "target_field": field_name},
            )
        if field_name == "名称":
            new_name = _text(raw_value)
            if not new_name:
                raise _missing(intent_id, "新名字")
            clash = self._find_product_by_name(tenant_id, new_name)
            if clash is not None and clash.product_id != product.product_id:
                raise AppError(
                    "E_VALIDATION",
                    message=f"已经有一个商品叫「{new_name}」了",
                    data={"intent_id": intent_id},
                )
            old_name = str(product.name)
            product.name = new_name
            self._session.flush()
            return EntityWriteResult(
                intent_id=intent_id,
                entity="product",
                entity_id=str(product.product_id),
                summary=f"商品「{old_name}」改名为「{new_name}」",
                created=False,
            )

        fen = _money_fen(raw_value)
        if fen is None or fen < 0:
            raise AppError(
                "E_VALIDATION",
                message=f"「{_text(raw_value) or '空'}」不是一个能识别的金额",
                data={"intent_id": intent_id, "target_field": field_name},
            )
        attribute = _PRICE_FIELDS[field_name]
        old_fen = int(getattr(product, attribute))
        setattr(product, attribute, fen)
        self._session.flush()
        return EntityWriteResult(
            intent_id=intent_id,
            entity="product",
            entity_id=str(product.product_id),
            summary=(
                f"商品「{product.name}」{field_name} {_fen_text(old_fen)} → {_fen_text(fen)}"
                "（只改之后的默认价，历史单据不回溯）"
            ),
            created=False,
        )

    def _upsert_product(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        unit: str,
        purchase_fen: int | None,
        sale_fen: int | None,
    ) -> tuple[Product, bool]:
        """按名字取商品；没有就建（**幂等**：同名不建第二条）。"""
        existing = self._find_product_by_name(tenant_id, name)
        if existing is not None:
            changed = False
            if purchase_fen is not None and int(existing.last_purchase_price_fen or 0) != purchase_fen:
                existing.last_purchase_price_fen = purchase_fen
                changed = True
            if sale_fen is not None and int(existing.last_sale_price_fen or 0) != sale_fen:
                existing.last_sale_price_fen = sale_fen
                changed = True
            if changed:
                self._session.flush()
            return existing, False
        product = tenant_repository(self._session, Product).add(
            Product(
                name=name,
                spec="",
                base_unit=unit or "个",
                last_purchase_price_fen=int(purchase_fen or 0),
                last_sale_price_fen=int(sale_fen or 0),
                status="active",
            )
        )
        self._session.flush()
        return product, True

    def _find_product_by_name(self, tenant_id: uuid.UUID, name: str) -> Product | None:
        """按名字查商品（**兼容规则层填进来的「展示名」**）。

        为什么不能只按全名查：规则层把 ``P`` 填成 ``display_name()``
        （``"可乐 500ml"``，见 ``template_engine``），而档案里 ``Product.name``
        只有 ``"可乐"``。只比全名 → 别名/改价/改名这些意图的卡片**一出生就 422**，
        提示还写着「没有找到商品 可乐 500ml」——用户看着商品明明在，会以为系统坏了。

        所以按「全名 → 去掉规格后的名字」两段查，先全名后短名（全名唯一性更高）。
        tenant 条件由 ORM 全局 scope + 显式 where 双重保证。
        """
        target = (name or "").strip()
        if not target:
            return None
        for candidate in _name_candidates(target):
            found = self._session.execute(
                select(Product)
                .where(Product.tenant_id == tenant_id, Product.name == candidate)
                .order_by(Product.status.asc())
                .limit(1)
            ).scalar_one_or_none()
            if found is not None:
                return found
        return None

    def _require_product(self, tenant_id: uuid.UUID, name: str, *, intent: str) -> Product:
        """取商品；不存在就报错（不自动建 —— 「改价」不该顺手建一个新商品）。"""
        if not name:
            raise _missing(intent, "商品名")
        product = self._find_product_by_name(tenant_id, name)
        if product is None:
            raise AppError(
                "E_VALIDATION",
                message=f"没有找到商品「{name}」；新建请说「新建一个 {name}」",
                data={"intent_id": intent, "name": name},
            )
        return product

    def _find_partner(
        self, tenant_id: uuid.UUID, partner_type: str, name: str
    ) -> Partner | None:
        """按（类型, 名字）查伙伴。"""
        target = (name or "").strip()
        if not target:
            return None
        return self._session.execute(
            select(Partner)
            .where(
                Partner.tenant_id == tenant_id,
                Partner.type == partner_type,
                Partner.name == target,
            )
            .limit(1)
        ).scalar_one_or_none()

    def _add_product_alias(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        product_id: uuid.UUID,
        alias_text: str,
    ) -> bool:
        """加商品别名（同一个人重复说同一句话不产生第二条）。"""
        existing = self._session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.tenant_id == tenant_id,
                ProductAlias.member_id == member_id,
                ProductAlias.alias_text == alias_text,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.product_id != product_id:
                raise AppError(
                    "E_VALIDATION",
                    message=f"「{alias_text}」已经指向另一个商品了；请先在大屏里改掉那条别名",
                    data={"intent_id": "product.alias.add"},
                )
            return False
        tenant_repository(self._session, ProductAlias).add(
            ProductAlias(
                product_id=product_id,
                member_id=member_id,
                alias_text=alias_text,
                source="personal",
            )
        )
        self._session.flush()
        return True

    def _add_conversion(
        self,
        *,
        tenant_id: uuid.UUID,
        product_id: uuid.UUID,
        from_unit: str,
        to_unit: str,
        ratio: Decimal,
    ) -> int:
        """写一条**待审批**的换算（与 R3 的 product.unit.set 同口径）。"""
        existing = self._session.execute(
            select(UnitConversion)
            .where(
                UnitConversion.tenant_id == tenant_id,
                UnitConversion.product_id == product_id,
                UnitConversion.from_unit == from_unit,
                UnitConversion.to_unit == to_unit,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return 0
        tenant_repository(self._session, UnitConversion).add(
            UnitConversion(
                product_id=product_id,
                from_unit=from_unit,
                to_unit=to_unit,
                ratio=ratio,
                status="pending",
            )
        )
        self._session.flush()
        return 1

    def _add_personal_dict(
        self,
        *,
        tenant_id: uuid.UUID,
        member_id: uuid.UUID,
        raw_text: str,
        mapped_type: str,
        mapped_id: uuid.UUID,
    ) -> bool:
        """写个人词表（``UNIQUE(tenant_id, member_id, raw_text, mapped_type)``）。"""
        existing = self._session.execute(
            select(PersonalDict)
            .where(
                PersonalDict.tenant_id == tenant_id,
                PersonalDict.member_id == member_id,
                PersonalDict.raw_text == raw_text,
                PersonalDict.mapped_type == mapped_type,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return False
        tenant_repository(self._session, PersonalDict).add(
            PersonalDict(
                member_id=member_id,
                layer="personal",
                raw_text=raw_text,
                mapped_type=mapped_type,
                mapped_id=mapped_id,
                version=0,
            )
        )
        self._session.flush()
        return True


# ---------------------------------------------------------------------------
# 工具（全部纯函数，便于单测）
# ---------------------------------------------------------------------------
#: 展示名里「名称」与「规格」之间的分隔符（``display_name()`` 用的是半角空格，
#: 但用户/LLM 偶尔会给全角空格或斜杠）
_NAME_SEPARATORS: tuple[str, ...] = (" ", "\u3000", "/", "／", "|", "｜")


def _name_candidates(target: str) -> tuple[str, ...]:
    """``"可乐 500ml"`` → ``("可乐 500ml", "可乐")``（**全名优先**）。

    纯粹做「去掉规格」的候选展开，不做模糊匹配——查不到就是查不到，
    交给上层的澄清/报错去问用户，不在这里猜。
    """
    candidates: list[str] = [target]
    for separator in _NAME_SEPARATORS:
        if separator not in target:
            continue
        head = target.split(separator, 1)[0].strip()
        if head and head not in candidates:
            candidates.append(head)
    return tuple(candidates)


def _text(value: Any) -> str:
    """槽位值转成去空白的字符串（``None`` → ``""``）。"""
    if value is None:
        return ""
    return str(value).strip()


#: 别名前缀里的连接词/系动词（**长的在前**，先匹配「叫做」再匹配「叫」）。
#:
#: 为什么要在意：LLM 抽槽位时经常把连接词一起吐出来——「可乐别名叫快乐水」
#: 会抽出 ``ALIAS='叫快乐水'``。不清掉的话，用户以后说「快乐水」解析不到
#: （词表里躺的是「叫快乐水」），**教了等于没教**；脏别名还会长期留在词表里。
_ALIAS_LEAD_CUES: tuple[str, ...] = ("叫做", "就是", "称为", "叫作", "叫", "是", "为", "成")


def _strip_alias_cue(text: str, *, product_name: str = "") -> str:
    """剥掉别名前面的连接词（只剥一次，剥完还是空就当没剥）。

    有一条保护：**商品名本身就以这个词开头时不动**（叫花鸡、成为…），
    否则会把别名的本体吃掉，把「叫花鸡」教成「花鸡」。

    Args:
        text: 原始别名。
        product_name: 商品真名（用来做上面那条保护）。

    Returns:
        剥掉连接词后的别名。
    """
    value = text.strip()
    for cue in _ALIAS_LEAD_CUES:
        if not value.startswith(cue):
            continue
        rest = value[len(cue) :].strip()
        if not rest:
            continue
        if product_name.startswith(cue):
            continue
        return rest
    return value


def _money_fen(value: Any) -> int | None:
    """金额槽位转「分」。

    ``UP`` / ``TA`` / ``AMT`` 按契约已经是 ``money_fen``（整数分）；
    ``TV`` 是 ``dynamic``，可能是「3 块 5」这种口语，交给 ``parse_cny_text``。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.isdigit() or (text_value.startswith("-") and text_value[1:].isdigit()):
        return int(text_value)
    return parse_cny_text(text_value)


def _number(value: Any) -> Decimal | None:
    """数值槽位转 ``Decimal``（``RATIO`` 等）。"""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    parsed = parse_quantity(str(value))
    if parsed.value is None:
        return None
    return Decimal(str(parsed.value))


def _number_text(value: Decimal) -> str:
    """``Decimal`` 去掉多余的 ``.0``。"""
    text_value = format(value.normalize(), "f")
    return text_value.rstrip("0").rstrip(".") if "." in text_value else text_value


def _fen_text(fen: int) -> str:
    """分 → ``¥x.xx``（回执展示用）。"""
    return f"¥{Decimal(int(fen)) / Decimal(100):.2f}"


def _partner_type(slots: Mapping[str, Any]) -> str:
    """由 ``C`` / ``S`` 哪个有值判定伙伴类型（两个都有 → 拒绝，不猜）。"""
    has_customer = bool(_text(slots.get("C")))
    has_supplier = bool(_text(slots.get("S")))
    if has_customer and has_supplier:
        raise AppError(
            "E_VALIDATION",
            message="同一句里既有客户又有供应商，分不清要建哪一个；请分开说",
            data={"intent_id": "partner.create"},
        )
    if has_supplier:
        return "supplier"
    if has_customer:
        return "customer"
    raise AppError(
        "E_VALIDATION",
        message="这是客户还是供应商？请说清「客户老王」或「供应商老张」",
        data={"intent_id": "partner.create"},
    )


def _partner_label(partner_type: str) -> str:
    """伙伴类型的中文名。"""
    return "供应商" if partner_type == "supplier" else "客户"


def _missing(intent_id: str, what: str) -> AppError:
    """缺槽位 → ``E_VALIDATION``（消息里点名缺什么）。"""
    return AppError(
        "E_VALIDATION",
        message=f"{intent_id} 缺少「{what}」，不能落库",
        data={"intent_id": intent_id, "missing": what},
    )
