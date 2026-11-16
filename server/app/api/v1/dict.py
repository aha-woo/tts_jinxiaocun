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
from app.models.dict import PersonalDict
from app.services.audit import AuditService
from app.services.dict_layers import DictVersionCache

router = APIRouter(prefix="/dict", tags=["dict"])

#: 全局版本缓存（进程内；多实例靠「缓存 key 带版本号」保证语义正确）
_VERSION_CACHE = DictVersionCache()

#: 允许教的映射类型（与 DB CHECK 一致）
_MAPPED_TYPES: frozenset[str] = frozenset({"product", "partner", "number", "unit", "intent"})


class TeachRequest(BaseModel):
    """教一个新说法。"""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=64)
    mapped_type: str = Field(default="product")
    mapped_id: uuid.UUID | None = None


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

    row = session.execute(
        select(PersonalDict)
        .where(
            PersonalDict.tenant_id == ids.tenant_id,
            PersonalDict.member_id == ids.member_id,
            PersonalDict.raw_text == payload.raw_text.strip(),
            PersonalDict.mapped_type == payload.mapped_type,
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = PersonalDict(
            tenant_id=ids.tenant_id,
            member_id=ids.member_id,
            layer="personal",
            raw_text=payload.raw_text.strip(),
            mapped_type=payload.mapped_type,
            mapped_id=payload.mapped_id,
            version=1,
        )
        session.add(row)
    else:
        row.version = int(row.version) + 1
        if payload.mapped_id is not None:
            row.mapped_id = payload.mapped_id
    session.flush()

    version = _VERSION_CACHE.bump(str(ids.tenant_id), str(ids.member_id))
    AuditService(session).record(
        tenant_id=ids.tenant_id,
        action="dict.teach",
        entity="personal_dict",
        entity_id=str(row.dict_id),
        member_id=ids.member_id,
        detail={"raw_text": row.raw_text, "mapped_type": row.mapped_type, "dict_version": version},
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "dict_id": str(row.dict_id),
                "raw_text": row.raw_text,
                "mapped_type": row.mapped_type,
                "dict_version": version,
                "effective_immediately": True,
                "note": "已写库并让解析缓存失效（下次说这句就能听懂）",
            }
        ),
    )


@router.get("", summary="我的说法本")
def list_dict(context: AuthContextDep, session: TenantSessionDep) -> JSONResponse:
    """列出当前员工教过的说法。"""
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
    return JSONResponse(
        status_code=200,
        content=ok(
            {
                "items": [
                    {
                        "dict_id": str(row.dict_id),
                        "raw_text": row.raw_text,
                        "mapped_type": row.mapped_type,
                        "mapped_id": None if row.mapped_id is None else str(row.mapped_id),
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
    """删除一条说法（销户时一并处理）。"""
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
