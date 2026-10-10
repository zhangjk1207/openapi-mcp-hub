"""演示模式测试：DEMO=1 自动注册固定 key 演示服务，幂等且不影响正常模式。"""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from openapi_mcp_hub.config import GatewayConfig
from openapi_mcp_hub.demo import DEMO_KEY, demo_enabled, maybe_register_demo
from openapi_mcp_hub.main import create_app
from openapi_mcp_hub.mcp_host import McpHost
from openapi_mcp_hub.registry import Registry
from openapi_mcp_hub.store import FileStore

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path):
    return GatewayConfig(admin_token="t", data_dir=tmp_path)


async def test_demo_disabled_by_default(tmp_path):
    assert not demo_enabled()
    reg = Registry(_cfg(tmp_path), McpHost(),
                   store=FileStore(tmp_path / "services"))
    await reg.connect_store()
    await maybe_register_demo(reg)
    assert reg.list_services() == []
    await reg.shutdown()


async def test_demo_registers_fixed_key_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAPI_MCP_HUB_DEMO", "1")
    monkeypatch.setenv("DEMO_PETSTORE_TOKEN", "demo-secret-token")
    monkeypatch.setenv("OPENAPI_MCP_HUB_DEMO_API_URL", "http://127.0.0.1:20034")

    reg = Registry(_cfg(tmp_path), McpHost(),
                   store=FileStore(tmp_path / "services"))
    await reg.connect_store()
    await maybe_register_demo(reg)
    svc = reg.get("petstore")
    assert svc.record["api_key_hash"] == hashlib.sha256(DEMO_KEY.encode()).hexdigest()
    tool_names = [t.name for t in svc.tools]
    assert "list_pets" in tool_names

    # 幂等：二次调用不炸、不覆盖
    await maybe_register_demo(reg)
    assert len(reg.list_services()) == 1

    # 固定 key 过网关鉴权（tools/list 不打目标 API）
    app = create_app(_cfg(tmp_path))
    reg2 = app.state.registry
    await reg2.connect_store()
    await reg2.load_from_store()
    await maybe_register_demo(reg2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp/petstore", headers={"X-Api-Key": DEMO_KEY},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert "list_pets" in names
    await reg2.shutdown()
