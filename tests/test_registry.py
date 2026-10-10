"""registry：注册 / 更新 / 下线 / 轮换 / 落盘恢复 / allowlist。"""

import hashlib
import json

import pytest

from openapi_mcp_hub.config import GatewayConfig
from openapi_mcp_hub.errors import RegistrationError, ServiceConflictError
from openapi_mcp_hub.mcp_host import McpHost
from openapi_mcp_hub.registry import Registry

from .conftest import make_registration


async def test_register_and_persist(gateway_cfg):
    host = McpHost()
    reg = Registry(gateway_cfg, host)
    svc, plain_key, warnings = await reg.register(make_registration())

    # 落盘检查
    path = gateway_cfg.data_dir / "services" / "petstore.json"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["name"] == "petstore"
    assert record["base_url"] == "http://target.test"
    assert record["api_key_hash"] == hashlib.sha256(plain_key.encode()).hexdigest()
    # spec 落盘的是原文（含 $ref）
    assert "$ref" in json.dumps(record["spec"])

    # 工具生成正确（internal 未被过滤时包含全部 5 个）
    assert len(svc.tools) == 6
    assert host.get("petstore") is not None
    await reg.shutdown()


async def test_register_conflict(gateway_cfg):
    reg = Registry(gateway_cfg, McpHost())
    await reg.register(make_registration())
    with pytest.raises(ServiceConflictError):
        await reg.register(make_registration())
    await reg.shutdown()


async def test_update_keeps_key_changes_tools(gateway_cfg):
    reg = Registry(gateway_cfg, McpHost())
    svc1, key1, _ = await reg.register(make_registration())

    updated = make_registration(
        filters={"include": ["/pets/{petId}"], "exclude": [], "exclude_tags": [], "include_deprecated": False}
    )
    svc2, _ = await reg.update("petstore", updated)
    assert [t.name for t in svc2.tools] == ["get_pet_by_id", "delete_pet_by_id"]
    # key 哈希与创建时间保留
    assert svc2.record["api_key_hash"] == svc1.record["api_key_hash"]
    assert svc2.record["created_at"] == svc1.record["created_at"]
    # 旧明文 key 仍可用
    assert svc2.record["api_key_hash"] == hashlib.sha256(key1.encode()).hexdigest()
    await reg.shutdown()


async def test_remove(gateway_cfg):
    reg = Registry(gateway_cfg, McpHost())
    await reg.register(make_registration())
    await reg.remove("petstore")
    assert not (gateway_cfg.data_dir / "services" / "petstore.json").exists()
    assert reg.list_services() == []
    with pytest.raises(Exception):
        reg.get("petstore")
    await reg.shutdown()


async def test_rotate_key(gateway_cfg):
    host = McpHost()
    reg = Registry(gateway_cfg, host)
    svc, key1, _ = await reg.register(make_registration())
    key2 = await reg.rotate_key("petstore")
    assert key2 != key1
    assert host._key_hashes["petstore"] == hashlib.sha256(key2.encode()).hexdigest()
    record = json.loads((gateway_cfg.data_dir / "services" / "petstore.json").read_text())
    assert record["api_key_hash"] == hashlib.sha256(key2.encode()).hexdigest()
    await reg.shutdown()


async def test_load_from_store_recovery(gateway_cfg):
    # 第一个网关实例：注册两个服务后关闭
    reg1 = Registry(gateway_cfg, McpHost())
    await reg1.register(make_registration(name="petstore"))
    await reg1.register(make_registration(name="another", base_url="http://other.test"))
    await reg1.shutdown()

    # 第二个实例：从盘上恢复
    host2 = McpHost()
    reg2 = Registry(gateway_cfg, host2)
    restored = await reg2.load_from_store()
    assert sorted(restored) == ["another", "petstore"]
    names = [s["name"] for s in reg2.list_services()]
    assert names == ["another", "petstore"]
    assert len(reg2.get("petstore").tools) == 6
    await reg2.shutdown()


async def test_load_from_store_skips_broken_file(gateway_cfg):
    services_dir = gateway_cfg.data_dir / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "broken.json").write_text("{ not json", encoding="utf-8")
    reg = Registry(gateway_cfg, McpHost())
    restored = await reg.load_from_store()
    assert restored == []
    await reg.shutdown()


async def test_allowlist(tmp_path):
    cfg = GatewayConfig(
        admin_token="t",
        data_dir=tmp_path,
        host_allowlist=["10.0.0.0/8", "api.internal"],
    )
    reg = Registry(cfg, McpHost())
    # 命中 CIDR
    await reg.register(make_registration(base_url="http://10.1.2.3:8080"))
    # 命中域名（含子域）
    await reg.register(make_registration(name="by-domain", base_url="https://v2.api.internal/x"))
    # 未命中 => 拒绝（IP 直接比对网段；未列名域名先做 DNS 解析核验，解析失败同样拒绝）
    with pytest.raises(RegistrationError, match="allowlist"):
        await reg.register(make_registration(name="bad", base_url="http://172.16.0.5:9000"))
    with pytest.raises(RegistrationError, match="allowlist|解析失败"):
        await reg.register(make_registration(name="bad2", base_url="http://evil-internal.io"))
    await reg.shutdown()


async def test_base_url_from_spec_servers(gateway_cfg):
    reg = Registry(gateway_cfg, McpHost())
    svc, _, warnings = await reg.register(
        make_registration(base_url=None)  # 未显式传 => 回落 spec.servers[0]
    )
    assert svc.record["base_url"] == "http://petstore.example.com"
    await reg.shutdown()


async def test_base_url_missing_rejected(tmp_path):
    cfg = GatewayConfig(admin_token="t", data_dir=tmp_path)
    reg = Registry(cfg, McpHost())
    spec = make_registration(base_url=None)
    spec.spec["servers"] = []  # 也没有 servers
    with pytest.raises(RegistrationError, match="base_url"):
        await reg.register(spec)
    await reg.shutdown()


async def test_allowed_tools_persisted_and_restored(gateway_cfg):
    """allowed_tools：落盘 + 重启恢复后仍生效；未命中的名字仅告警。"""
    host = McpHost()
    reg = Registry(gateway_cfg, host)
    svc, _, warnings = await reg.register(
        make_registration(allowed_tools=["get_pet_by_id", "bad_name"])
    )
    assert any("bad_name" in w for w in warnings)
    # list 层过滤
    result = await svc.app._on_list_tools(None, None)
    assert [t.name for t in result.tools] == ["get_pet_by_id"]
    # call 层拒绝白名单外工具
    from openapi_mcp_hub.mcp_host import CallToolRequestParams

    out = await svc.app._on_call_tool(
        None, CallToolRequestParams(name="list_pets", arguments={})
    )
    assert out.is_error
    assert "白名单" in out.content[0].text
    await reg.shutdown()

    # 重启恢复
    reg2 = Registry(gateway_cfg, McpHost())
    restored = await reg2.load_from_store()
    assert restored == ["petstore"]
    result = await reg2.get("petstore").app._on_list_tools(None, None)
    assert [t.name for t in result.tools] == ["get_pet_by_id"]
    await reg2.shutdown()


async def test_schema_defaults_filled_for_missing_args(gateway_cfg):
    """LLM 未传的可选参数填上 schema default（Unla fillDefaultArgs 语义）。"""
    from openapi_mcp_hub.mcp_host import CallToolRequestParams

    host = McpHost()
    reg = Registry(gateway_cfg, host)
    svc, _, _ = await reg.register(make_registration())
    # list_pets（spec 里 limit 自带 default: 20）
    import respx
    import httpx

    with respx.mock:
        route = respx.get("http://target.test/pets").mock(
            return_value=httpx.Response(200, json=[])
        )
        # LLM 一个参数都不传 → spec 里 limit 的 default=20 应被显式带给下游
        await svc.app._on_call_tool(
            None, CallToolRequestParams(name="list_pets", arguments={})
        )
        assert "limit=20" in str(route.calls.last.request.url)
    await reg.shutdown()
