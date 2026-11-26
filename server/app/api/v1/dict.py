"""我的说法本：`POST /dict/teach`、`GET /dict`、`DELETE /dict/{id}`。

``sys.teach`` 的承诺是**即时生效**：写库 + 版本号 +1 → 带旧版本的解析缓存自动失效。
否则用户教完还听不懂，会以为这东西坏了。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.api.v1._shared import assert_writable, ids_of, require_roles, WRITE_ROLES
from app.common.envelope import ok
from app.common.errors import AppError
from app.deps import AuthContextDep, TenantSessionDep
from app.models.catalog import Product, ProductAlias
from app.models.dict import PersonalDict
from app.models.partner import Partner
from app.services.audit import AuditService
from app.services.dict_layers import DictVersionCache

router = APIRouter(prefix="/dict", tags=["dict"])

#: 全局版本缓存（进程内；多实例靠「缓存 key 带版本号」保证语义正确）
_VERSION_CACHE = DictVersionCache()

#: 允许教的映射类型（与 DB CHECK 一致）
_MAPPED_TYPES: frozenset[str] = frozenset({"product", "partner", "number", "unit", "intent"})

MAPPED_TYPE_LABELS: dict[str, str] = {
    "product": "商品叫法",
    "partner": "往来单位",
    "number": "数字读法",
    "unit": "单位说法",
    "intent": "动作说法",
}


def _product_label(product: Product) -> str:
    spec = str(product.spec or "").strip()
    return f"{product.name} {spec}".strip() if spec else str(product.name)


def _resolve_mapped_names(
    session, tenant_id: uuid.UUID, rows: list[PersonalDict]
) -> dict[uuid.UUID, str]:
    """批量把 mapped_id 解析为可读的商品/往来单位名称，避免列表只看到孤立的别名。"""
    product_ids = [row.mapped_id for row in rows if row.mapped_id is not None and row.mapped_type == "product"]
    partner_ids = [row.mapped_id for row in rows if row.mapped_id is not None and row.mapped_type == "partner"]
    names: dict[uuid.UUID, str] = {}
    if product_ids:
        for product in session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.product_id.in_(product_ids))
        ).scalars():
            names[product.product_id] = _product_label(product)
    if partner_ids:
        for partner in session.execute(
            select(Partner).where(Partner.tenant_id == tenant_id, Partner.partner_id.in_(partner_ids))
        ).scalars():
            names[partner.partner_id] = str(partner.name)
    return names


def _resolve_target_id(
    session,
    *,
    tenant_id: uuid.UUID,
    mapped_type: str,
    target_name: str,
) -> tuple[uuid.UUID, str]:
    """根据用户输入的商品名/往来单位名反查对应实体 ID。"""
    target = target_name.strip()
    if mapped_type == "product":
        products = list(
            session.execute(
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.status.in_(("draft", "active")),
                )
            ).scalars()
        )
        matched = (
            next((p for p in products if str(p.name).strip() == target), None)
            or next((p for p in products if _product_label(p) == target), None)
            or next((p for p in products if target in _product_label(p)), None)
        )
        if matched is None:
            raise AppError("E_NOT_FOUND", message=f"店里还没找到商品「{target}」，请先建档或检查名称")
        return matched.product_id, _product_label(matched)

    if mapped_type == "partner":
        partners = list(
            session.execute(
                select(Partner).where(Partner.tenant_id == tenant_id, Partner.status == "active")
            ).scalars()
        )
        matched_partner = (
            next((p for p in partners if str(p.name).strip() == target), None)
            or next((p for p in partners if target in str(p.name)), None)
        )
        if matched_partner is None:
            raise AppError("E_NOT_FOUND", message=f"店里还没找到往来单位「{target}」")
        return matched_partner.partner_id, str(matched_partner.name)

    raise AppError("E_VALIDATION", message=f"当前类型「{mapped_type}」暂不支持按名称自动绑定")


class TeachRequest(BaseModel):
    """教一个新说法。"""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=64)
    mapped_type: str = Field(default="product")
    mapped_id: uuid.UUID | None = None
    target_name: str | None = Field(default=None, max_length=64)


@router.post("/teach", summary="教它一个说法（即时生效）")
def teach(
    payload: TeachRequest,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """写个人词表 + ``dict_version`` +1（击穿解析缓存）。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    if payload.mapped_type not in _MAPPED_TYPES:
        raise AppError(
            "E_VALIDATION",
            message=f"mapped_type 只能是 {sorted(_MAPPED_TYPES)}",
        )

    raw_text = payload.raw_text.strip()
    if not raw_text:
        raise AppError("E_VALIDATION", message="说法不能为空")

    resolved_mapped_id = payload.mapped_id
    resolved_mapped_name: str | None = None
    if resolved_mapped_id is None and payload.target_name and payload.target_name.strip():
        resolved_mapped_id, resolved_mapped_name = _resolve_target_id(
            session,
            tenant_id=ids.tenant_id,
            mapped_type=payload.mapped_type,
            target_name=payload.target_name,
        )

    row = session.execute(
        select(PersonalDict)
        .where(
            PersonalDict.tenant_id == ids.tenant_id,
            PersonalDict.member_id == ids.member_id,
            PersonalDict.raw_text == raw_text,
            PersonalDict.mapped_type == payload.mapped_type,
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = PersonalDict(
            tenant_id=ids.tenant_id,
            member_id=ids.member_id,
            layer="personal",
            raw_text=raw_text,
            mapped_type=payload.mapped_type,
            mapped_id=resolved_mapped_id,
            version=1,
        )
        session.add(row)
    else:
        row.version = int(row.version) + 1
        if resolved_mapped_id is not None:
            row.mapped_id = resolved_mapped_id

    # 商品叫法同步写入 ProductAlias，确保下句语音立刻可解析
    if payload.mapped_type == "product" and resolved_mapped_id is not None:
        alias = session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.tenant_id == ids.tenant_id,
                ProductAlias.member_id.is_(None),
                ProductAlias.alias_text == raw_text,
            )
            .limit(1)
        ).scalar_one_or_none()
        if alias is None:
            session.add(
                ProductAlias(
                    tenant_id=ids.tenant_id,
                    product_id=resolved_mapped_id,
                    member_id=None,
                    alias_text=raw_text,
                    source="personal",
                )
            )
        else:
            alias.product_id = resolved_mapped_id

    session.flush()
    if resolved_mapped_name is None and row.mapped_id is not None:
        resolved_mapped_name = _resolve_mapped_names(session, ids.tenant_id, [row]).get(row.mapped_id)

    version = _VERSION_CACHE.bump(str(ids.tenant_id), str(ids.member_id))
    AuditService(session).record(
        tenant_id=ids.tenant_id,
        action="dict.teach",
        entity="personal_dict",
        entity_id=str(row.dict_id),
        member_id=ids.member_id,
        detail={
            "raw_text": row.raw_text,
            "mapped_type": row.mapped_type,
            "mapped_name": resolved_mapped_name,
            "dict_version": version,
        },
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "dict_id": str(row.dict_id),
                "raw_text": row.raw_text,
                "mapped_type": row.mapped_type,
                "mapped_type_label": MAPPED_TYPE_LABELS.get(row.mapped_type, row.mapped_type),
                "mapped_id": None if row.mapped_id is None else str(row.mapped_id),
                "mapped_name": resolved_mapped_name,
                "dict_version": version,
                "effective_immediately": True,
                "note": "已写库并让解析缓存失效（下次说这句就能听懂）",
            }
        ),
    )


@router.get("", summary="我的说法本")
def list_dict(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """列出当前员工教过的说法（含对应商品/往来单位名称与中文分类）。"""
    ids = ids_of(context)
    rows = list(
        session.execute(
            select(PersonalDict)
            .where(
                PersonalDict.tenant_id == ids.tenant_id,
                PersonalDict.member_id == ids.member_id,
            )
            .order_by(PersonalDict.created_at.desc())
            .limit(200)
        ).scalars()
    )
    mapped_names = _resolve_mapped_names(session, ids.tenant_id, rows)
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [
                    {
                        "dict_id": str(row.dict_id),
                        "raw_text": row.raw_text,
                        "mapped_type": row.mapped_type,
                        "mapped_type_label": MAPPED_TYPE_LABELS.get(row.mapped_type, row.mapped_type),
                        "mapped_id": None if row.mapped_id is None else str(row.mapped_id),
                        "mapped_name": mapped_names.get(row.mapped_id) if row.mapped_id is not None else None,
                        "layer": row.layer,
                        "version": int(row.version),
                    }
                    for row in rows
                ],
                "dict_version": _VERSION_CACHE.get(str(ids.tenant_id), str(ids.member_id)),
            }
        ),
    )


@router.delete("/{dict_id}", summary="删掉一个说法")
def delete_dict(
    dict_id: uuid.UUID,
    context: AuthContextDep,
    session: TenantSessionDep,
) -> JSONResponse:
    """删除一条说法（销户时一并处理；商品别名同步解除绑定）。"""
    require_roles(context, WRITE_ROLES)
    ids = ids_of(context)
    assert_writable(session, ids.tenant_id)

    row = session.execute(
        select(PersonalDict)
        .where(
            PersonalDict.tenant_id == ids.tenant_id,
            PersonalDict.member_id == ids.member_id,
            PersonalDict.dict_id == dict_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise AppError("E_NOT_FOUND", message="这条说法不存在或不属于当前员工")
    if row.mapped_type == "product":
        session.execute(
            delete(ProductAlias).where(
                ProductAlias.tenant_id == ids.tenant_id,
                ProductAlias.alias_text == row.raw_text,
            )
        )
    session.execute(
        delete(PersonalDict).where(
            PersonalDict.tenant_id == ids.tenant_id, PersonalDict.dict_id == dict_id
        )
    )
    session.flush()
    version = _VERSION_CACHE.bump(str(ids.tenant_id), str(ids.member_id))
    AuditService(session).record(
        tenant_id=ids.tenant_id,
        action="dict.delete",
        entity="personal_dict",
        entity_id=str(dict_id),
        member_id=ids.member_id,
        detail={"dict_version": version},
    )
    return JSONResponse(
        status_code=200,
        content=ok({"dict_id": str(dict_id), "deleted": True, "dict_version": version}),
    )
