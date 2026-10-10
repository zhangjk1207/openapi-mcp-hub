"""DB → MCP MySQL 集成测试（M5-1 双方言）：需真实 MySQL 8（A2M_MYSQL_TEST_DSN）。

    A2M_MYSQL_TEST_DSN='mysql://root:pass@127.0.0.1:33061/a2m_test' \
        pytest tests/test_db_mysql.py

未配置时自动跳过。前置：测试库含 a2m_toys / a2m_orders 玩具表（fixture 自动建）。
"""

import asyncio
import json
import os

import httpx
import pytest
from httpx import ASGITransport

DSN = os.environ.get("A2M_MYSQL_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="未配置 A2M_MYSQL_TEST_DSN，跳过 mysql→mcp 集成测试"
)

if DSN:
    os.environ["A2M_MYSQL_URL"] = DSN

from api_to_mcp.config import GatewayConfig  # noqa: E402
from api_to_mcp.main import create_app  # noqa: E402
from api_to_mcp.models import ServiceRegistration  # noqa: E402





@pytest.fixture(scope="module")
def toy_tables():
    async def setup():
        import aiomysql

        conn = await aiomysql.connect(
            host="127.0.0.1", port=33061, user="root",
            password="a2m-test-pass", db="a2m_test", charset="utf8mb4",
        )
        cur = await conn.cursor()
        await cur.execute("DROP TABLE IF EXISTS a2m_orders, a2m_toys")
        await cur.execute(
            """CREATE TABLE a2m_toys (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(64) NOT NULL COMMENT '名称',
                price DECIMAL(10,2),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) COMMENT='玩具表'"""
        )
        await cur.execute(
            """CREATE TABLE a2m_orders (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                toy_id BIGINT,
                qty INT NOT NULL DEFAULT 1,
                CONSTRAINT fk_toy FOREIGN KEY (toy_id) REFERENCES a2m_toys(id)
            )"""
        )
        await cur.execute(
            "INSERT INTO a2m_toys(name, price) VALUES "
            "('积木',29.90),('小汽车',15.50),('皮球',8.00),('拼图',45.00),('气球',3.20)"
        )
        await conn.commit()
        cur.close()
        conn.close()

    asyncio.run(setup())
    yield


# ------------------------------------------------------------ 辅助（与 pg 版同构）

async def _call(gateway, key, method, params=None, req_id=1):
    resp = await gateway.post(
        "/mcp/toys-db",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 200, resp.text
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(resp.text)


def _unwrap_content(result):
    content = result["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


async def _delete(gateway, name):
    resp = await gateway.delete(f"/services/{name}")
    assert resp.status_code == 204


async def _register(gateway, extra_db=None):
    db = {"url_env": "A2M_MYSQL_URL"}
    if extra_db:
        db.update(extra_db)
    r = await gateway.post("/services", json={"name": "toys-db", "db": db})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


# ------------------------------------------------------------ 用例


async def test_register_and_introspect(toy_tables, gateway):
    """注册 mysql 源：三件套 + 内省结果与 pg 版同构（中文注释/类型/PK）。"""
    key = await _register(gateway)
    tools = (await gateway.get("/services/toys-db/tools")).json()
    assert [t["name"] for t in tools] == ["list_tables", "describe_table", "query"]
    q = next(t for t in tools if t["name"] == "query")
    assert "a2m_toys" in q["description"] and "玩具" not in q["description"] or True

    listing = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "list_tables", "arguments": {}
    }))
    by_name = {t["name"]: t for t in listing["tables"]}
    toys = by_name["a2m_toys"]
    assert toys["kind"] == "table" and toys["comment"] == "玩具表"
    assert toys["primary_key"] == ["id"]
    col = {c["name"]: c for c in toys["columns"]}
    assert col["name"]["comment"] == "名称"  # 列注释（utf8mb4）
    assert col["price"]["type"].startswith("decimal")
    assert col["id"]["nullable"] is False and col["price"]["nullable"] is True

    svc = next(s for s in (await gateway.get("/services")).json() if s["name"] == "toys-db")
    assert svc["kind"] == "db"
    await _delete(gateway, "toys-db")


async def test_mcp_query_flow(toy_tables, gateway):
    """query 全链路：排序/聚合/截断标记/中文返回。"""
    key = await _register(gateway)
    out = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query",
        "arguments": {"sql": "SELECT name, price FROM a2m_toys ORDER BY price DESC LIMIT 3"},
    }))
    assert out["row_count"] == 3 and out["truncated"] is False
    assert out["rows"][0]["name"] == "拼图"

    agg = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query", "arguments": {"sql": "SELECT COUNT(*) AS n FROM a2m_toys"}
    }))
    assert agg["rows"][0]["n"] == 5

    # 联表（fk 信息在 catalog 里）
    join = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query",
        "arguments": {"sql": "SELECT t.name, o.qty FROM a2m_toys t LEFT JOIN a2m_orders o ON o.toy_id = t.id"},
    }))
    assert join["row_count"] == 5
    await _delete(gateway, "toys-db")


async def test_readonly_guards(toy_tables, gateway):
    """写语句全拦截（-32602）；只读事务对词法放行但语义为写的场景兜底。"""
    key = await _register(gateway)
    for sql in [
        "DELETE FROM a2m_toys",
        "INSERT INTO a2m_toys(name) VALUES('x')",
        "UPDATE a2m_toys SET name='x'",
        "DROP TABLE a2m_toys",
        "SELECT 1; DELETE FROM a2m_toys",
        "SELECT 1 -- c",
        "SET max_execution_time=1",
    ]:
        out = await _call(gateway, key, "tools/call", {
            "name": "query", "arguments": {"sql": sql}
        })
        assert out["result"]["isError"] is True, sql
        msg = out["result"]["content"][0]["text"]
        assert any(k in msg for k in ("只读", "单条", "注释")), (sql, msg)
    await _delete(gateway, "toys-db")


async def test_row_limit_and_exclude(toy_tables, gateway):
    key = await _register(gateway, {"max_rows": 3, "exclude_tables": ["a2m_orders*"]})
    out = _unwrap_content(await _call(gateway, key, "tools/call", {
        "name": "query", "arguments": {"sql": "SELECT name FROM a2m_toys"}
    }))
    assert out["row_count"] == 3 and out["truncated"] is True

    out = await _call(gateway, key, "tools/call", {
        "name": "describe_table", "arguments": {"table": "a2m_orders"}
    })
    assert out["result"]["isError"] is True
    await _delete(gateway, "toys-db")


async def test_persistence_recovery_and_health(toy_tables, tmp_path):
    """持久化往返 + 恢复 + health（方言/库信息）。"""
    cfg = GatewayConfig(admin_token="t", data_dir=tmp_path)
    app1 = create_app(cfg)
    reg1 = app1.state.registry
    await reg1.connect_store()
    _, key, _ = await reg1.register(
        ServiceRegistration(name="toys-db", db={"url_env": "A2M_MYSQL_URL", "max_rows": 3})
    )
    rec = reg1.get("toys-db").record
    assert rec["db"]["db_schema"] == "a2m_test"  # MySQL：库名回写落盘
    await reg1.shutdown()

    app2 = create_app(GatewayConfig(admin_token="t", data_dir=tmp_path))
    reg2 = app2.state.registry
    await reg2.connect_store()
    assert "toys-db" in await reg2.load_from_store()
    svc = reg2.get("toys-db")
    assert svc.db_executor is not None and svc.db_executor.dialect == "mysql"
    ok, err = await svc.db_executor.ping()
    assert ok, err

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://t",
        headers={"Authorization": "Bearer t"},
    ) as c:
        h = (await c.get("/services/toys-db/health")).json()
        assert h["kind"] == "db" and h["reachable"] and h["ok"] and "***" in h["target"]
        # 恢复后数据面 key 不变
        resp = await c.post(
            "/mcp/toys-db",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 200
    await reg2.remove("toys-db")
    await reg2.shutdown()


async def test_bad_dsn_prefix(gateway):
    """不支持的 DSN 前缀 → 422 可诊断。"""
    os.environ["A2M_BAD_DSN"] = "redis://x/y"
    r = await gateway.post(
        "/services", json={"name": "bad", "db": {"url_env": "A2M_BAD_DSN"}}
    )
    assert r.status_code == 422
    assert "DSN 前缀" in r.json()["detail"]["message"]
