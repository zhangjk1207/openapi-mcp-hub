"""管理面 API：鉴权 / CRUD / 错误码（ASGI 内嵌测试，不起真实端口）。"""

import hashlib
import json

import httpx
import pytest
from httpx import ASGITransport

from api_to_mcp.main import create_app
from api_to_mcp.models import SERVICE_NAME_PATTERN

from .conftest import PETSTORE_SPEC, make_registration


@pytest.fixture
async def client_and_registry(gateway_cfg):
    app = create_app(gateway_cfg)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app.state.registry
    await app.state.registry.shutdown()


def _auth_headers(token: str = "test-admin-token") -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    async def test_no_token_configured_returns_503(self, tmp_path):
        from api_to_mcp.config import GatewayConfig

        app = create_app(GatewayConfig(admin_token=None, data_dir=tmp_path))
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post("/services", json=make_registration().model_dump())
        assert resp.status_code == 503
        await app.state.registry.shutdown()

    async def test_missing_or_wrong_token_401(self, client_and_registry):
        client, _ = client_and_registry
        resp = await client.post("/services", json=make_registration().model_dump())
        assert resp.status_code == 401
        resp = await client.post(
            "/services", json=make_registration().model_dump(), headers=_auth_headers("wrong")
        )
        assert resp.status_code == 401

    async def test_invalid_name_pattern_422(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration().model_dump()
        payload["name"] = "Bad_Name"  # 大写+下划线违反命名规则
        resp = await client.post("/services", json=payload, headers=_auth_headers())
        assert resp.status_code == 422


class TestCrud:
    async def test_register_full_response(self, client_and_registry):
        client, reg = client_and_registry
        resp = await client.post(
            "/services", json=make_registration().model_dump(), headers=_auth_headers()
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "petstore"
        assert data["mcp_url"].endswith("/mcp/petstore")
        assert data["api_key"].startswith("psk-")
        assert "get_pet_by_id" in data["tools"]
        # 响应直接给出可粘贴的 claude code 命令
        assert data["claude_code_cmd"].startswith("claude mcp add --transport http petstore")
        assert data["api_key"] in data["claude_code_cmd"]
        # 哈希落盘
        record = json.loads(
            (reg.cfg.data_dir / "services" / "petstore.json").read_text(encoding="utf-8")
        )
        assert record["api_key_hash"] == hashlib.sha256(data["api_key"].encode()).hexdigest()

    async def test_register_conflict_409(self, client_and_registry):
        client, _ = client_and_registry
        headers = _auth_headers()
        await client.post("/services", json=make_registration().model_dump(), headers=headers)
        resp = await client.post("/services", json=make_registration().model_dump(), headers=headers)
        assert resp.status_code == 409

    async def test_register_bad_spec_422(self, client_and_registry):
        client, _ = client_and_registry
        # Swagger 2.0 现已支持（自动归一化），改用真正非法的 spec
        bad = make_registration(spec={"openapi": "9.9", "paths": {"/x": {"get": {}}}})
        resp = await client.post("/services", json=bad.model_dump(), headers=_auth_headers())
        assert resp.status_code == 422
        assert "不支持的 openapi 版本" in resp.json()["detail"]["message"]

    async def test_register_spec_text_json(self, client_and_registry):
        """spec_text 收 JSON 文本（上传文件的通道），走同一流水线。"""
        client, reg = client_and_registry
        spec_text = json.dumps(json.loads(json.dumps(PETSTORE_SPEC)))
        r = make_registration(name="upload-json", spec=None, spec_text=spec_text)
        resp = await client.post("/services", json=r.model_dump(), headers=_auth_headers())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["tool_count"] == 6
        assert reg.get("upload-json").record["base_url"] == "http://target.test"

    async def test_register_spec_text_yaml(self, client_and_registry):
        """spec_text 收 YAML 文本：loader JSON→YAML 兜底解析生效。"""
        client, reg = client_and_registry
        spec_text = (
            "openapi: 3.0.0\n"
            "info: {title: Y, version: '1'}\n"
            "servers: [{url: 'http://yaml-svc.test'}]\n"
            "paths:\n"
            "  /a:\n"
            "    get:\n"
            "      operationId: list_a\n"
            "      responses: {'200': {description: ok}}\n"
        )
        r = make_registration(name="upload-yaml", spec=None, base_url=None, spec_text=spec_text)
        resp = await client.post("/services", json=r.model_dump(), headers=_auth_headers())
        assert resp.status_code == 201, resp.text
        assert resp.json()["tools"] == ["list_a"]
        assert reg.get("upload-yaml").record["base_url"] == "http://yaml-svc.test"

    async def test_spec_text_conflicts_422(self, client_and_registry):
        """spec / spec_text / spec_url 多源同传 → pydantic 校验 422（构造期即拒）。"""
        client, _ = client_and_registry
        payload = {"name": "multi-src", "spec_text": "openapi: 3.0.0",
                   "spec_url": "http://allowlisted.test/spec.json"}
        resp = await client.post("/services", json=payload, headers=_auth_headers())
        assert resp.status_code == 422
        assert "互斥" in resp.text

    async def test_spec_text_bad_format_422(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration(spec=None, spec_text='{"a": [unclosed').model_dump()
        resp = await client.post("/services", json=payload, headers=_auth_headers())
        assert resp.status_code == 422
        assert "JSON 也不是合法 YAML" in resp.json()["detail"]["message"]

    async def test_register_requires_spec(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration().model_dump()
        payload["spec"] = None
        payload["spec_url"] = None
        resp = await client.post("/services", json=payload, headers=_auth_headers())
        assert resp.status_code == 422

    async def test_list_detail_tools(self, client_and_registry):
        client, _ = client_and_registry
        headers = _auth_headers()
        await client.post("/services", json=make_registration().model_dump(), headers=headers)

        resp = await client.get("/services", headers=headers)
        assert resp.status_code == 200
        assert [s["name"] for s in resp.json()] == ["petstore"]
        assert resp.json()[0]["tool_count"] == 6

        resp = await client.get("/services/petstore", headers=headers)
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["base_url"] == "http://target.test"
        assert len(detail["tools"]) == 6

        resp = await client.get("/services/petstore/tools", headers=headers)
        tools = resp.json()
        by_name = {t["name"]: t for t in tools}
        assert "petId" in by_name["get_pet_by_id"]["inputSchema"]["required"]

        resp = await client.get("/services/none", headers=headers)
        assert resp.status_code == 404

    async def test_update_put(self, client_and_registry):
        client, _ = client_and_registry
        headers = _auth_headers()
        await client.post("/services", json=make_registration().model_dump(), headers=headers)
        updated = make_registration(
            filters={"include": ["/pets/{petId}"], "exclude": [], "exclude_tags": [], "include_deprecated": False}
        ).model_dump()
        resp = await client.put("/services/petstore", json=updated, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["tool_count"] == 2
        # PUT 后 tools 接口同步变化
        resp = await client.get("/services/petstore/tools", headers=headers)
        assert {t["name"] for t in resp.json()} == {"get_pet_by_id", "delete_pet_by_id"}

    async def test_delete(self, client_and_registry):
        client, reg = client_and_registry
        headers = _auth_headers()
        await client.post("/services", json=make_registration().model_dump(), headers=headers)
        resp = await client.delete("/services/petstore", headers=headers)
        assert resp.status_code == 204
        assert not (reg.cfg.data_dir / "services" / "petstore.json").exists()
        resp = await client.get("/services/petstore", headers=headers)
        assert resp.status_code == 404

    async def test_rotate_key(self, client_and_registry):
        client, _ = client_and_registry
        headers = _auth_headers()
        created = (await client.post("/services", json=make_registration().model_dump(), headers=headers)).json()
        resp = await client.post("/services/petstore/rotate-key", headers=headers)
        assert resp.status_code == 200
        new_key = resp.json()["api_key"]
        assert new_key != created["api_key"]
        assert new_key.startswith("psk-")


class TestPreview:
    async def test_preview_returns_tools_without_persisting(self, client_and_registry):
        """preview：完整流水线但不落盘、不生成端点/key（template-tester 平替）。"""
        client, reg = client_and_registry
        payload = make_registration(response_template="{{response.name}}").model_dump()
        resp = await client.post("/services/preview", json=payload, headers=_auth_headers())
        assert resp.status_code == 200
        out = resp.json()
        assert out["tool_count"] == len(out["tools"]) > 0
        assert out["base_url"] == "http://target.test"
        assert "api_key" not in out
        # 未落盘、未注册
        assert not (reg.cfg.data_dir / "services" / "petstore.json").exists()
        names = [s["name"] for s in (await client.get("/services", headers=_auth_headers())).json()]
        assert "petstore" not in names

    async def test_preview_with_sample_response_renders_templates(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration(
            response_template="宠物 {{response.name}}（{{response.id}}）"
        ).model_dump()
        payload["sample_response"] = {"name": "小白", "id": 7}
        resp = await client.post("/services/preview", json=payload, headers=_auth_headers())
        assert resp.status_code == 200
        previews = resp.json()["template_preview"]
        assert previews, "应至少渲染一个工具模板"
        assert all(v == "宠物 小白（7）" for v in previews.values())

    async def test_preview_bad_template_422(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration(response_template="{{response-a}}").model_dump()
        resp = await client.post("/services/preview", json=payload, headers=_auth_headers())
        assert resp.status_code == 422

    async def test_preview_unknown_allowed_tools_warned(self, client_and_registry):
        client, _ = client_and_registry
        payload = make_registration(allowed_tools=["get_pet_by_id", "no_such_tool"]).model_dump()
        resp = await client.post("/services/preview", json=payload, headers=_auth_headers())
        assert resp.status_code == 200
        assert any("no_such_tool" in w for w in resp.json()["warnings"])
        assert resp.json()["exposed_tool_count"] == 1


class TestCallsLog:
    async def test_calls_endpoint_after_invocation(self, client_and_registry):
        """调用日志：invoke 后 /calls 有一条 ok 记录；错误调用也留痕。"""
        client, reg = client_and_registry
        headers = _auth_headers()
        await client.post("/services", json=make_registration().model_dump(), headers=headers)
        svc = reg.get("petstore")

        from api_to_mcp.mcp_host import CallToolRequestParams
        from api_to_mcp.models import ApiBody, ApiEndpoint, ApiParam, ParamLocation, ToolDef

        ep = ApiEndpoint(method="get", path="/pets/{petId}", operation_id="getPetById",
                         params=[ApiParam("petId", ParamLocation.path, required=True,
                                          schema={"type": "integer"})])
        tool = ToolDef(name="get_pet_by_id", description="", input_schema={}, endpoint=ep)
        svc.app._tool_map[tool.name] = tool  # 注入一个直连 invoker 会 404 的工具

        # 错误调用（target.test 无真实服务 → 网络错误也留痕）
        await svc.app._on_call_tool(None, CallToolRequestParams(name=tool.name, arguments={"petId": 1}))
        resp = await client.get("/services/petstore/calls", headers=headers)
        assert resp.status_code == 200
        out = resp.json()
        assert out["count"] >= 1
        entry = out["calls"][0]
        assert entry["tool"] == "get_pet_by_id" and entry["status"] == "network"
        assert entry["error_code"] == -32002 and "duration_ms" in entry and "ts" in entry

        # 未知工具 → unknown_tool 记录
        await svc.app._on_call_tool(None, CallToolRequestParams(name="ghost", arguments={}))
        out = (await client.get("/services/petstore/calls", headers=headers)).json()
        assert out["calls"][0]["status"] == "unknown_tool"

    async def test_calls_404_for_missing_service(self, client_and_registry):
        client, _ = client_and_registry
        resp = await client.get("/services/ghost/calls", headers=_auth_headers())
        assert resp.status_code == 404
