"""DB → MCP（M5-1）集成测试：需要真实 PostgreSQL（OMH_PG_TEST_DSN 指向专用测试库）。

    OMH_PG_TEST_DSN='postgresql://user:pass@host:port/openapi_mcp_hub_test' \
        pytest tests/test_db_mcp.py

未配置时自动跳过。测试在目标库 public schema 下建/删 a2m_* 玩具表。
"""

import asyncio
import json
import os

import httpx
import pytest
from httpx import ASGITransport

from openapi_mcp_hub.config import GatewayConfig
from openapi_mcp_hub.main import create_app
from openapi_mcp_hub.models import ServiceRegistration

DSN = os.environ.get("OMH_PG_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="未配置 OMH_PG_TEST_DSN，跳过 db→mcp 集成测试"
)

# e2e 走本进程 env：测试库 DSN 作为演示服务的目标库
if DSN:
    os.environ["OMH_TEST_DB_URL"] = DSN




TOOLS_JSON = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {},
}


@pytest.fixture(scope="module")
def toy_tables():
    """在测试库建两张玩具表（模块级：建一次，测完删）。"""

    async def setup():
        import asyncpg

        conn = await asyncpg.connect(DSN)
        await conn.execute("DROP TABLE IF EXISTS a2m_toys, a2m_orders")
        await conn.execute(
            """
            CREATE TABLE a2m_toys (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                price NUMERIC(10,2),
                created_at TIMESTAMPTZ DEFAULT now()
            );
            COMMENT ON TABLE a2m_toys IS '玩具表';
            COMMENT ON COLUMN a2m_toys.name IS '名称';
            """
        )
        await conn.execute(
            """
            CREATE TABLE a2m_orders (
                id BIGSERIAL PRIMARY KEY,
                toy_id BIGINT REFERENCES a2m_toys(id),
                qty INT NOT NULL DEFAULT 1
            )
            """
        )
        await conn.executemany(
            "INSERT INTO a2m_toys(name, price) VALUES($1, $2)",
            [("积木", 29.9), ("小汽车", 15.5), ("皮球", 8.0), ("拼图", 45.0), ("气球", 3.2)],
        )
        await conn.close()

    asyncio.run(setup())
    yield
    asyncio.run(_drop())


async def _drop():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS a2m_orders, a2m_toys")
    await conn.close()


# ------------------------------------------------------------ 注册与工具生成


async def test_register_db_service_and_tools(toy_tables, gateway):
    """注册 db 源 → 三件套工具，list_tables 返回内省结果。"""
    reg = {
        "name": "toys-db",
        "db": {"url_env": "OMH_TEST_DB_URL"},
        "owner": "db-test",
    }
    resp = await gateway.post("/services", json=reg)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tool_count"] == 3
    assert "api_key" in body

    tools = (await gateway.get("/services/toys-db/tools")).json()
    names = [t["name"] for t in tools]
    assert names == ["list_tables", "describe_table", "query"]
    # query 工具 description 内嵌 schema 摘要（LLM 盲写 SQL 依据）
    q = next(t for t in tools if t["name"] == "query")
    assert "a2m_toys" in q["description"] and "a2m_orders" in q["description"]

    listing = (await gateway.get("/services")).json()
    svc = next(s for s in listing if s["name"] == "toys-db")
    assert svc["kind"] == "db" and svc["base_url"].startswith("db:public@")

    await _delete(gateway, "toys-db")


async def test_register_requires_env(toy_tables, gateway):
    """url_env 未配置 → 422，错误信息含变量名。"""
    resp = await gateway.post(
        "/services", json={"name": "bad-db", "db": {"url_env": "OMH_NOPE_URL"}}
    )
    assert resp.status_code == 422
    assert "OMH_NOPE_URL" in resp.json()["detail"]["message"]


async def test_register_spec_xor_db(gateway):
    resp = await gateway.post(
        "/services",
        json={
            "name": "both",
            "spec": {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {}},
            "db": {"url_env": "OMH_TEST_DB_URL"},
        },
    )
    assert resp.status_code == 422


# ------------------------------------------------------------ MCP 数据面


async def _call(gateway, key, method, params=None, req_id=1):
    resp = await gateway.post(
        "/mcp/toys-db",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 200, resp.text
    # streamable 响应可能是 text/event-stream
    text = resp.text
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


def _unwrap_content(result):
    content = result["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


async def _delete(gateway, name):
    resp = await gateway.delete(f"/services/{name}")
    assert resp.status_code == 204


async def test_mcp_full_flow(toy_tables, gateway):
    """全链路：注册 → tools/list → list_tables → describe_table → query。"""
    reg = await gateway.post(
        "/services", json={"name": "toys-db", "db": {"url_env": "OMH_TEST_DB_URL"}}
    )
    key = reg.json()["api_key"]

    tools = await _call(gateway, key, "tools/list")
    assert len(tools["result"]["tools"]) == 3

    listing = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "list_tables", "arguments": {}
    }))
    names = {t["name"] for t in listing["tables"]}
    assert {"a2m_toys", "a2m_orders"} <= names

    detail = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "describe_table", "arguments": {"table": "a2m_toys"}
    }))
    assert detail["primary_key"] == ["id"]
    col_names = [c["name"] for c in detail["columns"]]
    assert col_names == ["id", "name", "price", "created_at"]
    assert detail["comment"] == "玩具表"

    out = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query",
        "arguments": {"sql": "SELECT name, price FROM a2m_toys ORDER BY price DESC LIMIT 3"},
    }))
    assert out["row_count"] == 3 and out["truncated"] is False
    assert out["rows"][0]["name"] == "拼图"
    assert out["rows"][0]["price"] in (45.0, "45.00")  # numeric 经 default=str 序列化

    # describe 未知表
    bad = await _call(gateway, key, "tools/call", {
        "name": "describe_table", "arguments": {"table": "nope"}
    })
    assert bad["result"]["isError"] is True

    await _delete(gateway, "toys-db")


async def test_readonly_guards(toy_tables, gateway):
    """写语句 / 多语句 / 注释 → 全部词法拦截（-32602），错误信息可指导 LLM 改写。"""
    reg = await gateway.post(
        "/services", json={"name": "toys-db", "db": {"url_env": "OMH_TEST_DB_URL"}}
    )
    key = reg.json()["api_key"]

    for sql in [
        "DELETE FROM a2m_toys",
        "INSERT INTO a2m_toys(name) VALUES('x')",
        "UPDATE a2m_toys SET name='x'",
        "DROP TABLE a2m_toys",
        "TRUNCATE a2m_toys",
        "SELECT 1; DELETE FROM a2m_toys",
        "SELECT 1 -- comment",
    ]:
        out = await _call(gateway, key, "tools/call", {
            "name": "query", "arguments": {"sql": sql}
        })
        assert out["result"]["isError"] is True, sql
        msg = out["result"]["content"][0]["text"]
        assert any(k in msg for k in ("只读", "单条", "注释")), (sql, msg)

    # 只读事务兜底：词法放行但语义为写的场景（EXPLAIN 包装）也不可写
    out = await _call(gateway, key, "tools/call", {
        "name": "query",
        "arguments": {"sql": "SELECT count(*) FROM a2m_toys"},
    })
    assert _unwrap_content(out)["rows"][0]["count"] == 5

    await _delete(gateway, "toys-db")


async def test_row_limit_truncation(toy_tables, gateway):
    """max_rows 截断：表 5 行，上限 3 → truncated 标记。"""
    reg = await gateway.post(
        "/services",
        json={
            "name": "toys-db",
            "db": {"url_env": "OMH_TEST_DB_URL", "max_rows": 3},
        },
    )
    key = reg.json()["api_key"]
    out = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query", "arguments": {"sql": "SELECT name FROM a2m_toys"}
    }))
    assert out["row_count"] == 3 and out["truncated"] is True

    await _delete(gateway, "toys-db")


async def test_exclude_tables(toy_tables, gateway):
    """exclude 通配：排除后不进工具描述，describe 也不可见。"""
    reg = await gateway.post(
        "/services",
        json={
            "name": "toys-db",
            "db": {"url_env": "OMH_TEST_DB_URL", "exclude_tables": ["a2m_orders*"]},
        },
    )
    key = reg.json()["api_key"]
    tools = await _call(gateway, key, "tools/list")
    q = next(t for t in tools["result"]["tools"] if t["name"] == "query")
    assert "a2m_orders" not in q["description"]
    assert "a2m_toys" in q["description"]

    out = await _call(gateway, key, "tools/call", {
        "name": "describe_table", "arguments": {"table": "a2m_orders"}
    })
    assert out["result"]["isError"] is True

    await _delete(gateway, "toys-db")


async def test_persistence_and_recovery(toy_tables, tmp_path):
    """注册 db 服务 → 新 Registry（同库）load_from_store → 恢复且 key 不变。"""
    cfg = GatewayConfig(admin_token="t", data_dir=tmp_path)
    app1 = create_app(cfg)
    reg1 = app1.state.registry
    await reg1.connect_store()
    _, key, _ = await reg1.register(
        ServiceRegistration(name="toys-db", db={"url_env": "OMH_TEST_DB_URL", "max_rows": 3})
    )
    rec = reg1.get("toys-db").record
    assert rec["db"]["max_rows"] == 3  # db 配置完整落盘
    await reg1.shutdown()

    app2 = create_app(GatewayConfig(admin_token="t", data_dir=tmp_path))
    reg2 = app2.state.registry
    await reg2.connect_store()
    restored = await reg2.load_from_store()
    assert "toys-db" in restored
    svc = reg2.get("toys-db")
    assert svc.db_executor is not None and svc.invoker is None
    assert len(svc.tools) == 3

    # 恢复后直接可用：executor ping + 数据面 tools/list（key 不变）
    ok, err = await svc.db_executor.ping()
    assert ok, err

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://t"
    ) as c:
        resp = await c.post(
            "/mcp/toys-db",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 200
    await reg2.remove("toys-db")
    await reg2.shutdown()


async def test_health_db(toy_tables, gateway):
    reg = await gateway.post(
        "/services", json={"name": "toys-db", "db": {"url_env": "OMH_TEST_DB_URL"}}
    )
    assert reg.status_code == 201
    h = (await gateway.get("/services/toys-db/health")).json()
    assert h["kind"] == "db"
    assert h["reachable"] is True and h["auth_env_ok"] is True and h["ok"] is True
    assert "password" not in h["target"].lower() and "***" in h["target"]

    await _delete(gateway, "toys-db")


# ------------------------------------------------------------ 词法守卫回归（修复自查发现）


async def test_sql_literal_not_misjudged(toy_tables, gateway):
    """字符串字面量里的 -- / ; / /* 不再被误拦（词法扫描跳过字面量）。"""
    reg = await gateway.post(
        "/services", json={"name": "toys-db", "db": {"url_env": "OMH_TEST_DB_URL"}}
    )
    key = reg.json()["api_key"]
    # 注释列名（col_description 为"名称"）无 -- 场景，用字面量构造
    out = await _call(gateway, key, "tools/call", {
        "name": "query",
        "arguments": {"sql": "SELECT name FROM a2m_toys WHERE name LIKE '%--%' OR name = 'a;b'"},
    })
    assert out["result"]["isError"] is False, out
    out = await _call(gateway, key, "tools/call", {
        "name": "query", "arguments": {"sql": "EXPLAIN ANALYZE SELECT 1"}
    })
    assert out["result"]["isError"] is True
    assert "ANALYZE" in out["result"]["content"][0]["text"]
    await _delete(gateway, "toys-db")


async def test_register_rollback_when_store_fails(gateway_cfg, monkeypatch):
    """持久化失败：注册回滚（内存不留半激活服务），错误信息可诊断。"""
    from openapi_mcp_hub.config import GatewayConfig as GC
    from openapi_mcp_hub.main import create_app
    import openapi_mcp_hub.registry as reg_mod

    import httpx as _hx
    from httpx import ASGITransport as _T

    app = create_app(GC(admin_token="t", data_dir=gateway_cfg.data_dir))
    reg = app.state.registry
    await reg.connect_store()

    async def boom(record):
        raise RuntimeError("模拟存储故障")

    monkeypatch.setattr(reg._store, "save", boom)
    import os as _os
    _os.environ["OMH_TEST_DB_URL"] = "postgresql://x/y"
    from openapi_mcp_hub.models import ServiceRegistration as SR
    from openapi_mcp_hub.errors import RegistrationError

    # spec 为空即可触发构建前校验，用一个最小合法 spec 走完整注册路径
    spec = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}}}}
    try:
        await reg.register(SR(name="ghost", spec=spec, base_url="http://target.test"))
        raise AssertionError("应当抛 RegistrationError")
    except RegistrationError as e:
        assert "持久化失败" in str(e) and "已回滚" in str(e)
    # 内存不留半激活服务
    assert "ghost" not in reg._services and "ghost" not in reg.host._apps
    await reg.shutdown()
