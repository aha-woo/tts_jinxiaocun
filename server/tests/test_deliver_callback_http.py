"""发货回调的 **HTTP 装配**测试（不是在服务层直接传 session）。

上一轮为什么漏掉 403
--------------------
`tests/test_deliver_idempotency.py` 只测 `DeliverService`（自己 new session 传进去），
把「路由怎么拿到 session、怎么鉴权、租户从哪来」整段跳过了。于是端点用了
`TenantSessionDep`（要求 contextvar 已绑）却没有鉴权依赖 → **每次都是 403
`E_TENANT_DENIED`**，而 507 条测试全绿。付了钱的用户拿不到额度。

所以本文件一律走 **TestClient 打真实 HTTP 路径**，并断言**库里的终态**：
额度行有没有多出来、`deliver_event` 是不是 `delivered`、`usage_ledger` 有没有被误写。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models.pay import DeliverEvent, GoodsItem, VirtualOrder
from app.models.quota import UsageLedger, UsageQuota
from app.models.tenant import Tenant

TENANT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
GOODS_ID = "quota_pack_500"
OUT_TRADE_NO = "OT-HTTP-000001"
SERVICE_TOKEN = "svc-token-for-tests-only"


def _settings() -> Settings:
    """测试配置：内存库 + 服务间令牌 + 沙箱支付（验签器走 fake）。"""
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret="test-secret-for-deliver-http-do-not-reuse",
        wx_provider="fake",
        service_token=SERVICE_TOKEN,
        virtual_pay_env=0,
    )


@pytest.fixture()
def client():
    """真 app + 已种好的租户/道具/订单。"""
    application = create_app(_settings())
    database = application.state.database
    database.create_all()
    with database.session(bypass_scope=True) as session:
        session.add(Tenant(tenant_id=TENANT_ID, name="测试小店"))
        session.add(
            GoodsItem(
                goods_id=GOODS_ID,
                goods_name="语音加油包（500 次）",
                goods_type="quota_pack",
                price_fen=500,
                quota_amount=500,
                validity_days=0,
                status="on_shelf",
            )
        )
        session.add(
            VirtualOrder(
                tenant_id=TENANT_ID,
                out_trade_no=OUT_TRADE_NO,
                goods_id=GOODS_ID,
                price_fen=500,
                pay_status="pending",
                env="sandbox",
            )
        )
    with TestClient(application) as test_client:
        yield test_client
    database.drop_all()
    database.dispose()


def _body() -> dict[str, object]:
    """发货推送体（字段与 ``DeliverPayload.from_push`` 一致）。"""
    return {
        "out_trade_no": OUT_TRADE_NO,
        "goods_id": GOODS_ID,
        "tenant_id": str(TENANT_ID),
        "member_id": "member-1",
        "env": "sandbox",
        "timestamp": "1731628800",
        "nonce": "nonce-http-1",
        # fake 验签器的规则：sha256(f"{out_trade_no}|{goods_id}|{cert}")
        # 刻意做成「可预测但不可默认通过」——空签名是过不了的
        "signature": hashlib.sha256(
            f"{OUT_TRADE_NO}|{GOODS_ID}|FAKE-SERIAL-001".encode()
        ).hexdigest(),
    }


def _count(database, model) -> int:
    """数行数（绕过租户 scope，看的是全库真实值）。"""
    with database.session(bypass_scope=True) as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


# ---------------------------------------------------------------- 鉴权边界
def test_missing_service_token_is_rejected(client: TestClient) -> None:
    """没有服务间令牌 → 401，且**额度一行都不发**。"""
    response = client.post(
        "/api/v1/entitlement/deliver-callback",
        json=_body(),
        headers={"X-Wx-Channel": "cloudfunc"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["data"]["error_code"] == "E_AUTH_INVALID"
    assert _count(client.app.state.database, UsageQuota) == 0


def test_cloudrun_channel_is_rejected(client: TestClient) -> None:
    """云托管消息推送不是发货路径（两条通道都开着 = 必然重复发货）。"""
    response = client.post(
        "/api/v1/entitlement/deliver-callback",
        json=_body(),
        headers={"X-Wx-Channel": "cloudrun", "X-Service-Token": SERVICE_TOKEN},
    )
    assert response.status_code == 401, response.text
    assert _count(client.app.state.database, DeliverEvent) == 0


# ---------------------------------------------------------------- 正常发货
def test_valid_service_call_grants_quota_and_marks_event_delivered(client: TestClient) -> None:
    """正确令牌 + 云函数通道 → 额度真的到账（这条就是那 403 的回归钉）。"""
    response = client.post(
        "/api/v1/entitlement/deliver-callback",
        json=_body(),
        headers={"X-Wx-Channel": "cloudfunc", "X-Service-Token": SERVICE_TOKEN},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["delivered"] is True, f"reason={data['reason']} code={data['error_code']}"
    assert data["grant_quota"] == 500
    assert data["wrote_usage_ledger"] is False

    database = client.app.state.database
    # 终态 ①：额度行真的多了一条
    with database.session(bypass_scope=True) as session:
        quota = session.execute(
            select(UsageQuota).where(UsageQuota.tenant_id == TENANT_ID)
        ).scalar_one()
        event = session.execute(
            select(DeliverEvent).where(DeliverEvent.out_trade_no == OUT_TRADE_NO)
        ).scalar_one()
        print(
            f"\n[发货终态] usage_quota free_quota={quota.free_quota} period={quota.period} "
            f"deliver_event.deliver_status={event.deliver_status} grant_quota={event.grant_quota} "
            f"usage_ledger行数={_count(database, UsageLedger)}"
        )
        assert quota.free_quota >= 500
        assert str(event.deliver_status) == "delivered"
        assert int(event.grant_quota) == 500
    # 终态 ②：权益与语音扣次是两本账（A10），流水一行都不许写
    assert _count(database, UsageLedger) == 0


def test_repeated_push_does_not_double_grant(client: TestClient) -> None:
    """重复推送 → 幂等，不重复发额度。"""
    headers = {"X-Wx-Channel": "cloudfunc", "X-Service-Token": SERVICE_TOKEN}
    first = client.post("/api/v1/entitlement/deliver-callback", json=_body(), headers=headers)
    second = client.post("/api/v1/entitlement/deliver-callback", json=_body(), headers=headers)

    assert first.json()["data"]["delivered"] is True
    assert second.json()["data"]["delivered"] is False
    assert second.json()["data"]["error_code"] == "E_DELIVER_DUPLICATE"
    database = client.app.state.database
    assert _count(database, DeliverEvent) == 1
    with database.session(bypass_scope=True) as session:
        quota = session.execute(
            select(UsageQuota).where(UsageQuota.tenant_id == TENANT_ID)
        ).scalar_one()
    assert quota.free_quota == 500, "重复推送把额度发了两次"
