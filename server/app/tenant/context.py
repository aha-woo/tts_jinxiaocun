"""请求级租户上下文（contextvar）—— 九道防线第 3 条。

要点
----
- 上下文只在**单个请求/任务**内有效，用 ``bind()`` 设置、``reset(token)`` 复位；
- **缺失即抛错**，绝不默认「查全表」（漏加条件的最大风险点）；
- 异步并发安全：contextvar 按 task 隔离，`asyncio.gather` 下两个租户互不串味；
- 异步队列 / 定时任务 / 导出 Job 必须显式 ``with use_context(ctx):``，不允许裸跑。

本模块刻意只依赖标准库，避免与 ORM 形成循环导入。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class TenantContext:
    """一次请求/任务内的租户身份（全部来自服务端 JWT，业务入参禁止传）。"""

    tenant_id: uuid.UUID
    store_id: uuid.UUID
    member_id: uuid.UUID
    role_code: str
    tenant_version: int

    def as_dict(self) -> dict[str, object]:
        """日志/审计用字典（不含任何密钥）。"""
        return {
            "tenant_id": str(self.tenant_id),
            "store_id": str(self.store_id),
            "member_id": str(self.member_id),
            "role_code": self.role_code,
            "tenant_version": self.tenant_version,
        }


class TenantContextMissing(RuntimeError):
    """需要租户上下文却缺失时抛出（对应错误码 ``E_TENANT_DENIED``）。"""

    error_code = "E_TENANT_DENIED"

    def __init__(self, detail: str = "缺少租户上下文：禁止在无 tenant_id 的情况下执行数据访问") -> None:
        super().__init__(detail)
        self.detail = detail


_current: ContextVar[TenantContext | None] = ContextVar("vil_tenant_context", default=None)


def bind(context: TenantContext) -> Token[TenantContext | None]:
    """绑定上下文，返回用于复位的 token。"""
    return _current.set(context)


def reset(token: Token[TenantContext | None]) -> None:
    """按 token 复位（务必放在 finally 里）。"""
    _current.reset(token)


def clear() -> None:
    """清空当前 task 的上下文（测试/收尾用）。"""
    _current.set(None)


def get() -> TenantContext | None:
    """取当前上下文；无则返回 None（仅供「可缺省」的判断，如健康检查）。"""
    return _current.get()


def require() -> TenantContext:
    """取当前上下文；无则抛 :class:`TenantContextMissing`。"""
    context = _current.get()
    if context is None:
        raise TenantContextMissing()
    return context


def is_bound() -> bool:
    """是否已绑定上下文。"""
    return _current.get() is not None


@contextmanager
def use_context(context: TenantContext) -> Iterator[TenantContext]:
    """同步/异步任务里显式携带租户上下文。

    Example:
        >>> with use_context(ctx):
        ...     repo.list()
    """
    token = bind(context)
    try:
        yield context
    finally:
        reset(token)


def context_from_claims(claims: dict[str, object]) -> TenantContext:
    """从 JWT claims 还原上下文（类型转换集中在此，避免散落各处）。

    Raises:
        ValueError: claims 缺字段或 uuid 非法。
    """
    try:
        return TenantContext(
            tenant_id=uuid.UUID(str(claims["tenant_id"])),
            store_id=uuid.UUID(str(claims["store_id"])),
            member_id=uuid.UUID(str(claims["member_id"])),
            role_code=str(claims["role"]),
            tenant_version=int(claims["tenant_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"JWT claims 不完整或格式非法: {exc}") from exc
