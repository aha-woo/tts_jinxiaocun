"""v1 接口集合（架构 §5.1 的 24 个端点）。

| 模块 | 端点 |
|---|---|
| ``health`` | `/healthz`、`/readyz`（T01） |
| ``auth`` | `POST /auth/login`（T02） |
| ``voice`` | `POST /voice/sessions`、`/voice/sessions/{id}/parse`、`/voice/sessions/{id}/confirm2`（T03/T04） |
| ``clarify`` | `POST /voice/sessions/{id}/clarify` |
| ``cards`` | `GET /voice/cards/{id}`、`PATCH /voice/cards/{id}/edit`、`GET /voice/cards/pending` |
| ``confirm`` | `POST /voice/cards/{id}/confirm`、`POST /voice/cards/{id}/cancel` |
| ``reversal`` | `POST /docs/{id}/reversal`、`GET /docs/{id}/references` |
| ``query`` | `GET /query/{stock,docs,debt,report}`（R0 只读） |
| ``usage`` | `GET /usage`（自然月口径） |
| ``dict`` | `POST /dict/teach`、`GET /dict`、`DELETE /dict/{id}` |
| ``exports`` | `POST /exports`、`GET /exports`（R3） |
| ``tasks`` | `GET /tasks`、`POST /tasks/{id}/approve`（R3，仅大屏） |
| ``account`` | `POST/DELETE/GET /account/deletion`（R3，7 天冷静期） |
| ``entitlement`` | `/entitlement/{order,deliver-callback,goods,query-order}`、`GET /entitlement` |
| ``upload`` | `POST /upload/ticket`（只收引用，100KiB 断言） |

新增接口时请同步 ``docs/02-架构设计.md`` §5.1 的 API 清单。
"""

from app.api.v1 import (
    account,
    auth,
    cards,
    clarify,
    confirm,
    delivery_note,
    dict,
    entitlement,
    exports,
    health,
    counts,
    inventory,
    me,
    products,
    query,
    reversal,
    tasks,
    upload,
    usage,
    voice,
)

__all__ = [
    "account",
    "auth",
    "cards",
    "clarify",
    "confirm",
    "delivery_note",
    "dict",
    "entitlement",
    "exports",
    "health",
    "counts",
    "inventory",
    "me",
    "products",
    "query",
    "reversal",
    "tasks",
    "upload",
    "usage",
    "voice",
]
