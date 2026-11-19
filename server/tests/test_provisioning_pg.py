"""首次登录开通租户的 **真 PG** 回归测试（SQLite 上永远发现不了的外键顺序问题）。

背景（2026-11-19 云托管首次联调的真实故障）
----------------------------------------
``AuthService._provision`` 先一次性 ``session.add()`` 了 tenant / store / member /
role / entitlement / usage_quota，最后统一 ``flush()``。

SQLAlchemy 的 unit-of-work 在**没有** ``relationship()`` 时**不保证按外键顺序**发
INSERT，真 PostgreSQL 上它先发了 ``INSERT INTO member``，于是：

    psycopg.errors.ForeignKeyViolation: insert or update on table "member"
    violates foreign key constraint "fk_member_tenant_id_tenant"
    DETAIL: Key is not present in table "tenant".

SQLite 默认不校验外键，所以这条路径在本地测试里**永远是绿的**——只有真 PG 才暴露。

修复
----
按依赖顺序显式 flush：tenant → store/member → 其余。

为什么单独一个文件
------------------
放进 SQLite 套件没有意义（测不出来）；这里用 ``PG_TEST_URL`` 门控，和
``test_rls_cross_tenant.py`` 同一口径。不设变量时整文件 skip，不拖慢默认全量。
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.config import get_settings
from app.db.session import Database
from app.services.auth import AuthService
from app.services.wx_provider import WxIdentity

PG_TEST_URL = os.getenv("PG_TEST_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_TEST_URL,
    reason=(
        "未设置 PG_TEST_URL（需要真 PostgreSQL）。SQLite 不校验外键，"
        "测不出「先插 member 后插 tenant」这类顺序问题。"
        "跑法：PG_TEST_URL='postgresql+psycopg://user:pwd@host:5432/db' pytest tests/test_provisioning_pg.py"
    ),
)


class _FakeWxProvider:
    """固定 openid 的假 provider（避免每条用例重复开通同一租户）。"""

    def __init__(self) -> None:
        self.openid = f"openid-provision-{uuid.uuid4().hex[:12]}"

    def resolve_openid(self, code: str) -> WxIdentity:
        assert code, "登录 code 不能为空"
        return WxIdentity(openid=self.openid, unionid=None, session_key=None)


def test_first_login_provisions_tenant_before_member() -> None:
    """首次登录必须能成功开通（tenant 先于 member 落库）。"""
    database = Database.from_url(PG_TEST_URL)
    settings = get_settings()

    result = AuthService(database, settings, _FakeWxProvider()).login("fake-code")

    assert result.tenant_id is not None
    assert result.member_id is not None
    assert result.store_id is not None
    assert result.role == "boss"
    assert result.token


def test_second_login_reuses_the_same_tenant() -> None:
    """同一 openid 再登录一次：不能再开一个新租户，也不能撞唯一约束。"""
    database = Database.from_url(PG_TEST_URL)
    settings = get_settings()
    provider = _FakeWxProvider()

    first = AuthService(database, settings, provider).login("fake-code")
    second = AuthService(database, settings, provider).login("fake-code")

    assert first.tenant_id == second.tenant_id
    assert first.member_id == second.member_id
