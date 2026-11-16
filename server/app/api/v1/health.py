"""健康检查接口。

两个端点，职责严格分开（T02 起）：

- ``GET /healthz``：**存活探针**。只报进程状态 + 共享契约计数（T01 已冻结的结构，
  后续任务不得改动既有字段，避免端侧/网关解析口径漂移）；
- ``GET /readyz``：**就绪探针**。额外探测数据库连通、表数、RLS 能力自描述与
  JWT 密钥是否仍是本地默认值。DB/RLS 状态只在这里出现。

如实声明
--------
``readyz`` 的 ``isolation_mode`` 在非 PG 方言下**必然是** ``app_scope_only``，
即「没有 DB 级 RLS，隔离强度下降」—— 不允许为了好看而声称 RLS 仍在生效。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.common.envelope import ok
from app.common.idempotency import HybridStore
from app.config import EXPECTED_TABLES, ContractBundle, Settings
from app.db.session import Database
from app.deps import get_app_settings, get_contract_bundle, get_database, get_store

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="健康检查 + 契约计数")
async def healthz(
    settings: Settings = Depends(get_app_settings),
    contracts: ContractBundle = Depends(get_contract_bundle),
    store: HybridStore = Depends(get_store),
) -> JSONResponse:
    """返回服务状态、版本与共享契约计数。

    Returns:
        包络 ``{code: 0, data: {status, version, contract:{...}}, message: "ok"}``
    """
    counts = contracts.counts()
    payload = {
        "status": "ok",
        "version": settings.version,
        "env": settings.env,
        "checked_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contract": {
            "intents": counts["intents"],
            "slots": counts["slots"],
            "sources": counts["sources"],
            "error_codes": counts["error_codes"],
            "domains": counts["domains"],
        },
        "idempotency": await store.describe(),
        "capabilities": {
            "call_container": True,
            "max_request_body_bytes": settings.max_request_body_bytes,
            "web_client": False,
            "memo": "同一 AppID：手机端 + 电脑版微信大屏（resizable 双栏），无独立网页端",
        },
    }
    return JSONResponse(status_code=200, content=ok(payload))


@router.get("/readyz", summary="就绪检查 + 数据库/RLS 能力自描述")
async def readyz(
    settings: Settings = Depends(get_app_settings),
    contracts: ContractBundle = Depends(get_contract_bundle),
    database: Database = Depends(get_database),
) -> JSONResponse:
    """返回数据库连通性、表数与隔离能力（**不**混进 ``/healthz``）。

    ``status`` 语义：
    - ``ok``：DB 可连、表数符合预期、隔离能力自描述可读；
    - ``degraded``：DB 不可连 / 表数为 0 / JWT 仍在用本地默认密钥。
      非 PG 方言（本地 SQLite）**不算** degraded —— 那是设计内的「应用层隔离模式」，
      但会在 ``isolation_mode`` 里如实写明 ``app_scope_only``。
    """
    counts = contracts.counts()
    ping_ok = database.ping()
    capabilities = database.capabilities()
    tables = capabilities.get("tables", 0)
    table_count_ok = int(tables) == EXPECTED_TABLES
    jwt_default = settings.jwt_secret_is_default()

    degraded_reasons: list[str] = []
    if not ping_ok:
        degraded_reasons.append("database_unreachable")
    if not table_count_ok:
        degraded_reasons.append(f"table_count_mismatch(expect {EXPECTED_TABLES}, got {tables})")
    if jwt_default:
        degraded_reasons.append("jwt_secret_is_default")

    payload: dict[str, Any] = {
        "status": "degraded" if degraded_reasons else "ok",
        "version": settings.version,
        "env": settings.env,
        "checked_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contract": dict(counts),
        "database": {
            "reachable": ping_ok,
            "dialect": capabilities.get("dialect"),
            "tables": tables,
            "tables_expected": EXPECTED_TABLES,
            "tables_ok": table_count_ok,
        },
        "isolation": {
            "rls_enabled": capabilities.get("rls_enabled"),
            "isolation_mode": capabilities.get("isolation_mode"),
            "notes": capabilities.get("notes"),
            # 如实标注：非 PG 下隔离强度下降，只剩应用层 scope + 唯一索引 + CI 扫描 + 越权单测
            "strength": "db_rls" if capabilities.get("rls_enabled") else "reduced_one_layer_less",
        },
        "auth": {
            "jwt_algorithm": settings.jwt_algorithm,
            "jwt_secret_is_default": jwt_default,
            "wx_provider": settings.wx_provider,
        },
        "degraded_reasons": degraded_reasons,
    }
    http_status = 200 if ping_ok and table_count_ok else 503
    return JSONResponse(status_code=http_status, content=ok(payload))
