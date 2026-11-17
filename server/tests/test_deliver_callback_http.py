"""发货回调的 **HTTP 装配**测试（不是在服务层直接传 session）。

上一轮为什么漏掉 403
--------------------
`tests/test_deliver_idempotency.py` 只测 `DeliverService`（自己 new session 传进去），
把「路由怎么拿到 session、怎么鉴权、租户从哪来」整段跳过了。于是端点用了
`TenantSessionDep`（要求 contextvar 已绑）却没有鉴权依赖 → **每次都是 403
`E_TENANT_DENIED`**，而 507 条测试全绿。付了钱的用户拿不到额度。

所以本文件一律走 **TestClient 打真实 HTTP 路径**，并断言**库里的终态**：
`entitlement` / `usage_quota` 到底有没有多出来、`deliver_event` 是不是 `delivered`、
`usage_ledger` 有没有被误写。

本文件覆盖的四类（P0-3 的验收口径）
----------------------------------
1. 正确令牌 → 200 且**权益真的到账**（贴 `select(Entitlement)` 的实际值）；
2. 缺令牌 / 错令牌 / 错通道 → 403，且 `deliver_event` 0 行、额度一分没动；
3. 服务端没配令牌 → 503（**不是** 401/403，也不默认放行），并打 ERROR 告警；
4. 重复推送 → 1402 且额度只加一次。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import contextmanager
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models.pay import DeliverEvent, Entitlement, GoodsItem, VirtualOrder
from app.models.quota import UsageLedger, UsageQuota
from app.models.tenant import Tenant

TENANT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
#: 加油包（只加 usage_quota 额度）
QUOTA_GOODS_ID = "quota_pack_500"
QUOTA_OUT_TRADE_NO = "OT-HTTP-000001"
#: 权益包（延长 entitlement + 加额度）
PLAN_GOODS_ID = "plan_30day"
PLAN_OUT_TRADE_NO = "OT-HTTP-000002"
SERVICE_TOKEN = "svc-token-for-tests-only"
FAKE_CERT = "FAKE-SERIAL-001"

CALLBACK_PATH = "/api/v1/entitlement/deliver-callback"


def _settings(*, service_token: str = SERVICE_TOKEN) -> Settings:
    """测试配置：内存库 + 服务间令牌 + 沙箱支付（验签器走 fake）。"""
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-deliver-http-do-not-reuse",
        wx_provider="fake",
        service_token=service_token,
        virtual_pay_env=0,
    )


@contextmanager
def _app_client(*, service_token: str = SERVICE_TOKEN) -> Iterator[TestClient]:
    """建 app + 种好租户/道具/订单，退出时销毁。"""
    application = create_app(_settings(service_token=service_token))
    database = application.state.database
    database.create_all()
    with database.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_ID, name="测试小店"))
        session.add(
            GoodsItem(
                goods_id=QUOTA_GOODS_ID,
                goods_name="语音加油包（500 次）",
                goods_type="quota_pack",
                price_fen=500,
                quota_amount=500,
                validity_days=0,
                status="on_shelf",
            )
        )
        session.add(
            GoodsItem(
                goods_id=PLAN_GOODS_ID,
                goods_name="30 天畅聊包（1000 次）",
                goods_type="entitlement",
                price_fen=1900,
                quota_amount=1000,
                validity_days=30,
                status="on_shelf",
            )
        )
        session.add(
            VirtualOrder(
                tenant_id=TENANT_ID,
                out_trade_no=QUOTA_OUT_TRADE_NO,
                goods_id=QUOTA_GOODS_ID,
                price_fen=500,
                pay_status="pending",
                env="sandbox",
            )
        )
        session.add(
            VirtualOrder(
                tenant_id=TENANT_ID,
                out_trade_no=PLAN_OUT_TRADE_NO,
                goods_id=PLAN_GOODS_ID,
                price_fen=1900,
                pay_status="pending",
                env="sandbox",
            )
        )
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        database.drop_all()
        database.dispose()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """已配置服务间令牌的 app。"""
    with _app_client() as test_client:
        yield test_client


def _body(
    *,
    out_trade_no: str = QUOTA_OUT_TRADE_NO,
    goods_id: str = QUOTA_GOODS_ID,
    nonce: str = "nonce-http-1",
) -> dict[str, object]:
    """发货推送体（字段与 ``DeliverPayload.from_push`` 一致）。"""
    return {
        "out_trade_no": out_trade_no,
        "goods_id": goods_id,
        "tenant_id": str(TENANT_ID),
        "member_id": "member-1",
        "env": "sandbox",
        "timestamp": "1731628800",
        "nonce": nonce,
        # fake 验签器的规则：sha256(f"{out_trade_no}|{goods_id}|{cert}")
        # 刻意做成「可预测但不可默认通过」——空签名是过不了的
        "signature": hashlib.sha256(f"{out_trade_no}|{goods_id}|{FAKE_CERT}".encode()).hexdigest(),
    }


def _good_headers(token: str = SERVICE_TOKEN) -> dict[str, str]:
    """一个通过服务间鉴权的请求头组合。"""
    return {
        "X-Wx-Channel": "cloudfunc",
        "X-Service-Token": token,
        "X-Tenant-Id": str(TENANT_ID),
    }


def _count(database, model) -> int:
    """数行数（绕过租户 scope，看的是全库真实值）。"""
    with database.session(bypass_scope=True) as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_nothing_granted(database) -> None:
    """403/503 之后必须一条都没落：事件、额度、权益全为 0。"""
    assert _count(database, DeliverEvent) == 0, "被拒的请求竟然落了 deliver_event"
    assert _count(database, UsageQuota) == 0, "被拒的请求竟然加了额度"
    assert _count(database, Entitlement) == 0, "被拒的请求竟然发了权益"


# ---------------------------------------------------------------- 鉴权边界（403）
def test_missing_service_token_is_rejected(client: TestClient) -> None:
    """没有服务间令牌 → 403，且**额度一行都不发**。"""
    response = client.post(CALLBACK_PATH, json=_body(), headers={"X-Wx-Channel": "cloudfunc"})

    assert response.status_code == 403, response.text
    assert response.json()["data"]["error_code"] == "E_TENANT_DENIED"
    assert response.json()["data"]["reason"] == "service_token_mismatch"
    _assert_nothing_granted(client.app.state.database)


def test_wrong_service_token_is_rejected(client: TestClient) -> None:
    """错令牌 → 403（与缺令牌同一状态码/同一句话，不给探测信号）。"""
    response = client.post(
        CALLBACK_PATH, json=_body(), headers=_good_headers("not-the-right-token")
    )

    assert response.status_code == 403, response.text
    assert response.json()["data"]["reason"] == "service_token_mismatch"
    _assert_nothing_granted(client.app.state.database)


def test_cloudrun_channel_is_rejected(client: TestClient) -> None:
    """云托管消息推送不是发货路径（两条通道都开着 = 必然重复发货）。"""
    headers = _good_headers()
    headers["X-Wx-Channel"] = "cloudrun"
    response = client.post(CALLBACK_PATH, json=_body(), headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["data"]["reason"] == "channel_not_allowed"
    _assert_nothing_granted(client.app.state.database)


# ------------------------------------------------------- 服务端未配令牌（503）
def test_unconfigured_service_token_returns_503(caplog: pytest.LogCaptureFixture) -> None:
    """服务端没配 SERVICE_TOKEN → 503 + ERROR 告警（**不默认放行**）。

    为什么不是 401/403：问题在服务端配置，调用方凭据是对的；用 503 才能让
    「微信重试 + 运维告警」这条链正确触发，而不是让云函数以为「令牌写错了」。
    """
    caplog.set_level(logging.ERROR, logger="app.deps")
    with _app_client(service_token="") as client:
        response = client.post(CALLBACK_PATH, json=_body(), headers=_good_headers())

        assert response.status_code == 503, response.text
        assert response.json()["data"]["error_code"] == "E_INTERNAL"
        assert response.json()["data"]["reason"] == "service_token_not_configured"
        _assert_nothing_granted(client.app.state.database)

    messages = [record.message for record in caplog.records if record.name == "app.deps"]
    assert any("SERVICE_TOKEN" in message for message in messages), (
        f"未配置令牌必须打 ERROR 告警，实际日志: {messages}"
    )


# ---------------------------------------------------------------- 正常发货（200）
def test_valid_service_call_grants_entitlement(client: TestClient) -> None:
    """正确令牌 + 云函数通道 → **权益真的到账**（`select(Entitlement)` 的实际值）。"""
    response = client.post(
        CALLBACK_PATH,
        json=_body(out_trade_no=PLAN_OUT_TRADE_NO, goods_id=PLAN_GOODS_ID),
        headers=_good_headers(),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["delivered"] is True, f"reason={data['reason']} code={data['error_code']}"
    assert data["grant_quota"] == 1000
    assert data["extend_days"] == 30
    assert data["wrote_usage_ledger"] is False
    assert data["tenant_id"] == str(TENANT_ID), "租户必须来自订单反查，不是请求头"

    database = client.app.state.database
    with database.session(bypass_scope=True) as session:
        entitlement = session.execute(
            select(Entitlement).where(Entitlement.tenant_id == TENANT_ID)
        ).scalar_one()
        event = session.execute(
            select(DeliverEvent).where(DeliverEvent.out_trade_no == PLAN_OUT_TRADE_NO)
        ).scalar_one()
        print(
            "\n[发货终态] entitlement:"
            f" plan_code={entitlement.plan_code} quota_granted={entitlement.quota_granted}"
            f" remaining={entitlement.remaining} status={entitlement.status}"
            f" valid_from={entitlement.valid_from.isoformat()}"
            f" valid_until={entitlement.valid_until.isoformat()}"
            f" | deliver_event: status={event.deliver_status} grant_quota={event.grant_quota}"
            f" extend_days={event.extend_days}"
            f" | usage_ledger 行数={_count(database, UsageLedger)}"
        )
        # 终态 ①：权益行的**数值**（不是「有没有这张卡」）
        assert entitlement.plan_code == "30day"
        assert int(entitlement.quota_granted) == 1000
        assert int(entitlement.remaining) == 1000
        assert str(entitlement.status) == "active"
        assert entitlement.valid_until > entitlement.valid_from
        # 终态 ②：事件行
        assert str(event.deliver_status) == "delivered"
        assert int(event.grant_quota) == 1000
    # 终态 ③：权益与语音扣次是两本账（A10），流水一行都不许写
    assert _count(database, UsageLedger) == 0


def test_valid_service_call_grants_quota_pack(client: TestClient) -> None:
    """加油包口径：只给当月 `usage_quota.free_quota` 加额度，**不写 usage_ledger**。"""
    response = client.post(CALLBACK_PATH, json=_body(), headers=_good_headers())

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["delivered"] is True, f"reason={data['reason']} code={data['error_code']}"
    assert data["grant_quota"] == 500
    assert data["wrote_usage_ledger"] is False

    database = client.app.state.database
    with database.session(bypass_scope=True) as session:
        quota = session.execute(
            select(UsageQuota).where(UsageQuota.tenant_id == TENANT_ID)
        ).scalar_one()
        event = session.execute(
            select(DeliverEvent).where(DeliverEvent.out_trade_no == QUOTA_OUT_TRADE_NO)
        ).scalar_one()
        print(
            f"\n[发货终态] usage_quota free_quota={quota.free_quota} used={quota.used_count}"
            f" period={quota.period} | deliver_event={event.deliver_status}"
        )
        assert int(quota.free_quota) == 500
        assert int(quota.used_count) == 0
    assert _count(database, UsageLedger) == 0


def test_repeated_push_does_not_double_grant(client: TestClient) -> None:
    """重复推送 → 1402 幂等，额度只加一次。"""
    first = client.post(CALLBACK_PATH, json=_body(), headers=_good_headers())
    second = client.post(CALLBACK_PATH, json=_body(), headers=_good_headers())

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["delivered"] is True
    assert second.json()["data"]["delivered"] is False
    assert second.json()["data"]["error_code"] == "E_DELIVER_DUPLICATE"
    assert second.json()["code"] == 1402, second.json()

    database = client.app.state.database
    assert _count(database, DeliverEvent) == 1
    with database.session(bypass_scope=True) as session:
        quota = session.execute(
            select(UsageQuota).where(UsageQuota.tenant_id == TENANT_ID)
        ).scalar_one()
    assert int(quota.free_quota) == 500, "重复推送把额度发了两次"


# ------------------------------------------------------- 订单反查失败的边界
def test_unknown_out_trade_no_is_rejected_without_granting(client: TestClient) -> None:
    """单号查不到订单 → 404，不猜租户、不发额度。"""
    response = client.post(
        CALLBACK_PATH,
        json=_body(out_trade_no="OT-NOT-EXIST-999"),
        headers=_good_headers(),
    )

    assert response.status_code == 404, response.text
    assert response.json()["data"]["reason"] == "order_not_found"
    _assert_nothing_granted(client.app.state.database)


def test_missing_out_trade_no_is_validation_error(client: TestClient) -> None:
    """没有单号 → 422（无法定位订单，直接拒）。"""
    body = _body()
    body.pop("out_trade_no")
    response = client.post(CALLBACK_PATH, json=body, headers=_good_headers())

    assert response.status_code == 422, response.text
    assert response.json()["data"]["reason"] == "missing_out_trade_no"
    _assert_nothing_granted(client.app.state.database)
