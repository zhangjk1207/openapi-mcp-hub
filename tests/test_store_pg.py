"""PgStore 集成测试：需要真实 PostgreSQL（A2M_PG_TEST_DSN 指向专用测试库）。

    A2M_PG_TEST_DSN='postgresql://user:pass@host:port/api_to_mcp_test' pytest tests/test_store_pg.py

未配置时自动跳过，不影响常规测试。
"""

import json
import os

import httpx
import pytest
from httpx import ASGITransport

DSN = os.environ.get("A2M_PG_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="未配置 A2M_PG_TEST_DSN，跳过 pg 持久化集成测试"
)

RECORD = {
    "name": "petstore",
    "title": "宠物店",
    "base_url": "http://target.test",
    "api_key_hash": "h" * 64,
    "owner": "tester",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-02T00:00:00+00:00",
    "auth": {"type": "bearer", "token_env": "TOK"},
    "filters": {"include": [], "exclude": ["/internal/*"]},
    "response_template": "宠物 {{response.name}}",
    "allowed_tools": ["list_pets"],
    "spec": {"openapi": "3.1.0", "info": {"title": "宠物店", "version": "1"}, "paths": {}},
}


@pytest.fixture
async def pg_store(tmp_path):
    from api_to_mcp.store import PgStore

    store = PgStore(DSN, legacy_services_dir=tmp_path / "services")
    await store.connect()
    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM services")  # 测试间清表
    yield store
    await store.close()


async def test_save_load_roundtrip(pg_store):
    await pg_store.save(RECORD)
    out = await pg_store.load_all()
    assert len(out) == 1
    loaded = out[0]
    # JSONB 保真：中文、嵌套 spec、全部字段
    assert loaded == RECORD


async def test_upsert_same_name(pg_store):
    await pg_store.save(RECORD)
    updated = {**RECORD, "title": "宠物店 v2", "updated_at": "2026-01-03T00:00:00+00:00"}
    await pg_store.save(updated)
    out = await pg_store.load_all()
    assert len(out) == 1 and out[0]["title"] == "宠物店 v2"


async def test_delete(pg_store):
    await pg_store.save(RECORD)
    await pg_store.delete("petstore")
    assert await pg_store.load_all() == []


async def test_auto_migrate_from_files(pg_store, tmp_path):
    """首次切换 pg：表空 + 文件目录有记录 → 自动导入。"""
    legacy = tmp_path / "services"
    legacy.mkdir()
    (legacy / "petstore.json").write_text(json.dumps(RECORD, ensure_ascii=False), "utf-8")
    (legacy / "another.json").write_text(json.dumps({**RECORD, "name": "another"}), "utf-8")

    from api_to_mcp.store import PgStore

    store2 = PgStore(DSN, legacy_services_dir=legacy)
    await store2.connect()
    try:
        names = sorted(r["name"] for r in await store2.load_all())
        assert names == ["another", "petstore"]
        # 原文件保留（可回滚）
        assert (legacy / "petstore.json").exists()
    finally:
        await store2.close()


async def test_gateway_full_cycle_with_pg(tmp_path, monkeypatch):
    """端到端：pg 模式注册 → 重建 app（模拟重启）→ 服务恢复且 key 不变。"""
    from api_to_mcp.config import GatewayConfig
    from api_to_mcp.main import create_app

    monkeypatch.setenv("A2M_TEST_TOK", "x")
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    cfg = GatewayConfig(admin_token="t", data_dir=tmp_path, database_url=DSN)
    app1 = create_app(cfg)
    async with httpx.AsyncClient(transport=ASGITransport(app=app1), base_url="http://t") as c:
        await app1.state.registry.connect_store()
        resp = await c.post(
            "/services",
            json={
                "name": "pg-svc",
                "spec": spec,
                "base_url": "http://target.test",
                "auth": {"type": "bearer", "token_env": "A2M_TEST_TOK"},
                "owner": "pg-test",
            },
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 201, resp.text
        key = resp.json()["api_key"]
    await app1.state.registry.shutdown()

    # "重启"：新 app 实例，同一 DSN
    cfg2 = GatewayConfig(admin_token="t", data_dir=tmp_path / "fresh", database_url=DSN)
    app2 = create_app(cfg2)
    async with httpx.AsyncClient(transport=ASGITransport(app=app2), base_url="http://t") as c:
        await app2.state.registry.connect_store()
        restored = await app2.state.registry.load_from_store()
        assert "pg-svc" in restored
        tools = await c.get("/services/pg-svc/tools", headers={"Authorization": "Bearer t"})
        assert tools.status_code == 200
        # 数据面 key 不变（agent 侧无感）
        raw = await c.post(
            "/mcp/pg-svc",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "X-Api-Key": key,
                "Accept": "application/json, text/event-stream",
            },
        )
        assert raw.status_code == 200
        await app2.state.registry.remove("pg-svc")  # 清理测试数据
    await app2.state.registry.shutdown()
