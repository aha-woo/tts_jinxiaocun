"""测试「说法本」对应商品/往来单位名称解析、alias_count 去重、以及按 target_name 教说法与同步删除。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.catalog import ProductAlias
from app.models.dict import PersonalDict
from app.models.tenant import Tenant
from app.services.voice_runtime import reset_shared_voice_state
from scripts.seed_rich_demo import seed_tenant


def _create_client():
    reset_shared_voice_state()
    settings = Settings(
        env="test",
        wx_provider="fake",
        jwt_secret="test-secret-for-dict-mapping-32bytes",
        database_url="sqlite+pysqlite:///:memory:",
        llm_provider="fake",
        vision_provider="fake",
        asr_provider="fake",
    )
    app = create_app(settings=settings)
    app.state.database.create_all()
    return app, TestClient(app)


def test_dict_list_shows_target_name_and_me_dedups_alias_count() -> None:
    app, client = _create_client()
    with client:
        r = client.post("/api/v1/auth/login", json={"code": "wx-dict-boss"})
        assert r.status_code == 200
        login_data = r.json()["data"]
        headers = {"Authorization": f"Bearer {login_data['jwt']}"}
        tid = uuid.UUID(login_data["tenant_id"])

        with app.state.database.session(bypass_scope=True) as s:
            tenant = s.get(Tenant, tid)
            assert tenant is not None
        seed_tenant(app.state.database, tenant)

        # 1) /me 的 alias_count 去重后应为 4（不是 PersonalDict + ProductAlias 重复相加的 8）
        me_res = client.get("/api/v1/me", headers=headers)
        assert me_res.status_code == 200
        assert me_res.json()["data"]["alias_count"] == 4

        # 2) /dict 应返回对应的商品全名和中文分类标签
        dict_res = client.get("/api/v1/dict", headers=headers)
        assert dict_res.status_code == 200
        items = dict_res.json()["data"]["items"]
        assert len(items) == 4
        by_raw = {row["raw_text"]: row for row in items}
        assert by_raw["大水"]["mapped_name"] == "农夫山泉 550ml"
        assert by_raw["快乐水"]["mapped_name"] == "可乐 500ml"
        assert by_raw["红罐"]["mapped_name"] == "红牛 250ml"
        assert by_raw["烟"]["mapped_name"] == "软中华 硬条"
        assert all(row["mapped_type_label"] == "商品叫法" for row in items)

        # 3) 按 target_name 教一个新说法，应同时绑定 mapped_id 并同步写 ProductAlias
        teach_res = client.post(
            "/api/v1/dict/teach",
            headers=headers,
            json={"raw_text": "肥宅水", "mapped_type": "product", "target_name": "可乐"},
        )
        assert teach_res.status_code == 200, teach_res.text
        teach_data = teach_res.json()["data"]
        assert teach_data["mapped_name"] == "可乐 500ml"
        assert teach_data["mapped_type_label"] == "商品叫法"

        dict_after = client.get("/api/v1/dict", headers=headers).json()["data"]["items"]
        taught = next(row for row in dict_after if row["raw_text"] == "肥宅水")
        assert taught["mapped_name"] == "可乐 500ml"

        # 4) 删除说法时应同步清除对应的 ProductAlias 与 PersonalDict
        del_res = client.delete(f"/api/v1/dict/{taught['dict_id']}", headers=headers)
        assert del_res.status_code == 200
        with app.state.database.session(bypass_scope=True) as s:
            assert (
                s.execute(
                    select(PersonalDict).where(
                        PersonalDict.tenant_id == tid, PersonalDict.raw_text == "肥宅水"
                    )
                ).scalar_one_or_none()
                is None
            )
            assert (
                s.execute(
                    select(ProductAlias).where(
                        ProductAlias.tenant_id == tid, ProductAlias.alias_text == "肥宅水"
                    )
                ).scalar_one_or_none()
                is None
            )

        # 5) 找不到目标商品时明确提示 404，而不是落一条无指向的空映射
        bad_teach = client.post(
            "/api/v1/dict/teach",
            headers=headers,
            json={"raw_text": "不存在的别称", "mapped_type": "product", "target_name": "无此商品"},
        )
        assert bad_teach.status_code == 404
