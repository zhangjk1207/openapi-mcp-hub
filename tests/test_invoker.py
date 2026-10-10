"""invoker：HTTP 还原、认证注入、错误映射、截断（respx mock）。"""

import base64
import json

import httpx
import pytest
import respx

from openapi_mcp_hub.invoker import Invoker, ServiceHttpConfig
from openapi_mcp_hub.models import ApiBody, ApiEndpoint, ApiParam, ParamLocation, ToolDef

BASE = "http://target.test"


def _tool(method="get", path="/pets/{petId}", params=None, body=None, op_id="op") -> ToolDef:
    ep = ApiEndpoint(
        method=method,
        path=path,
        operation_id=op_id,
        params=params if params is not None else [
            ApiParam("petId", ParamLocation.path, required=True, schema={"type": "integer"})
        ],
        body=body,
    )
    return ToolDef(name=op_id, description="", input_schema={}, endpoint=ep)


class TestRequestBuilding:
    @respx.mock
    async def test_get_with_path_and_query(self):
        route = respx.get(f"{BASE}/pets/42").mock(return_value=httpx.Response(200, json={"id": 42}))
        tool = _tool(
            params=[
                ApiParam("petId", ParamLocation.path, required=True, schema={"type": "integer"}),
                ApiParam("limit", ParamLocation.query, required=False, schema={"type": "integer"}),
                ApiParam("tags", ParamLocation.query, required=False, schema={"type": "array"}),
            ]
        )
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(tool, {"petId": 42, "limit": 5, "tags": ["a", "b"]})
        assert result.ok and result.payload == {"id": 42}
        request = route.calls.last.request
        assert str(request.url) == f"{BASE}/pets/42?limit=5&tags=a&tags=b"

    @respx.mock
    async def test_path_param_encoded(self):
        route = respx.get(f"{BASE}/pets/a%2Fb").mock(return_value=httpx.Response(200, json={}))
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(_tool(), {"petId": "a/b"})
        assert result.ok
        # url.path 会解码，raw_path 保留百分号编码
        assert route.calls.last.request.url.raw_path == b"/pets/a%2Fb"

    @respx.mock
    async def test_post_json_body(self):
        route = respx.post(f"{BASE}/pets").mock(return_value=httpx.Response(201, json={"id": 1}))
        tool = _tool(
            method="post",
            path="/pets",
            params=[],
            body=ApiBody(schema={"type": "object"}, required=True),
        )
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(tool, {"body": {"name": "哈咪", "tag": "猫"}})
        assert result.ok and result.status_code == 201
        assert json.loads(route.calls.last.request.content) == {"name": "哈咪", "tag": "猫"}
        assert route.calls.last.request.headers["content-type"].startswith("application/json")

    @respx.mock
    async def test_post_urlencoded_form(self):
        """urlencoded body：按表单字段提交（Content-Type 由 httpx 设置）。"""
        route = respx.post(f"{BASE}/pets").mock(return_value=httpx.Response(201, json={"id": 1}))
        tool = _tool(
            method="post",
            path="/pets",
            params=[],
            body=ApiBody(
                schema={"type": "object"}, required=True, media_type="application/x-www-form-urlencoded"
            ),
        )
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(tool, {"body": {"name": "哈咪", "tag": "猫 狗"}})
        assert result.ok
        req = route.calls.last.request
        assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
        from urllib.parse import parse_qs

        assert parse_qs(req.content.decode()) == {"name": ["哈咪"], "tag": ["猫 狗"]}

    @respx.mock
    async def test_post_multipart_with_file(self):
        """multipart：普通字段走 data，{filename, content} 走文件，base64 自动解码。"""
        route = respx.post(f"{BASE}/upload").mock(return_value=httpx.Response(200, json={"ok": True}))
        tool = _tool(
            method="post",
            path="/upload",
            params=[],
            body=ApiBody(schema={"type": "object"}, required=True, media_type="multipart/form-data"),
        )
        inv = Invoker(ServiceHttpConfig(BASE, None))
        payload = {
            "caption": "小白",
            "photo": {
                "filename": "dog.txt",
                "content": "5Lit5Zu9",
                "encoding": "base64",
                "content_type": "text/plain",
            },
        }
        result = await inv.invoke(tool, {"body": payload})
        assert result.ok
        req = route.calls.last.request
        assert req.headers["content-type"].startswith("multipart/form-data")
        body = req.content.decode()
        assert 'name="caption"' in body and "小白" in body
        assert 'name="photo"; filename="dog.txt"' in body
        assert "中国" in body  # base64 已解码

    @respx.mock
    async def test_header_and_cookie_params(self):
        route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, json={}))
        tool = _tool(
            path="/x",
            params=[
                ApiParam("X-Trace", ParamLocation.header, schema={"type": "string"}),
                ApiParam("session", ParamLocation.cookie, schema={"type": "string"}),
            ],
        )
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(tool, {"X-Trace": "t1", "session": "s1"})
        assert result.ok
        headers = route.calls.last.request.headers
        assert headers["x-trace"] == "t1"
        assert headers["cookie"] == "session=s1"

    async def test_missing_path_param(self):
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(_tool(), {})
        assert not result.ok
        assert "petId" in result.error


class TestAuth:
    @respx.mock
    async def test_bearer_from_env(self, monkeypatch):
        monkeypatch.setenv("TOK", "tok-123")
        route = respx.get(f"{BASE}/pets/1").mock(return_value=httpx.Response(200, json={}))
        tool = _tool(params=[ApiParam("petId", ParamLocation.path, required=True)])
        from openapi_mcp_hub.models import AuthConfig

        inv = Invoker(ServiceHttpConfig(BASE, AuthConfig(type="bearer", token_env="TOK")))
        result = await inv.invoke(tool, {"petId": 1})
        assert result.ok
        assert route.calls.last.request.headers["authorization"] == "Bearer tok-123"

    async def test_bearer_env_missing(self, monkeypatch):
        monkeypatch.delenv("NOPE_TOK", raising=False)
        from openapi_mcp_hub.models import AuthConfig

        inv = Invoker(ServiceHttpConfig(BASE, AuthConfig(type="bearer", token_env="NOPE_TOK")))
        result = await inv.invoke(_tool(params=[ApiParam("petId", ParamLocation.path, required=True)]), {"petId": 1})
        assert not result.ok and "NOPE_TOK" in result.error

    @respx.mock
    async def test_api_key_header_and_query(self, monkeypatch):
        monkeypatch.setenv("AK", "ak-1")
        from openapi_mcp_hub.models import AuthConfig

        route = respx.get(f"{BASE}/pets/1").mock(return_value=httpx.Response(200, json={}))
        tool = _tool(params=[ApiParam("petId", ParamLocation.path, required=True)])
        inv = Invoker(
            ServiceHttpConfig(
                BASE, AuthConfig(type="api_key", key_name="X-API-Key", location="header", value_env="AK")
            )
        )
        assert (await inv.invoke(tool, {"petId": 1})).ok
        assert route.calls.last.request.headers["x-api-key"] == "ak-1"

        route2 = respx.get(f"{BASE}/pets/2").mock(return_value=httpx.Response(200, json={}))
        inv2 = Invoker(
            ServiceHttpConfig(
                BASE, AuthConfig(type="api_key", key_name="api_key", location="query", value_env="AK")
            )
        )
        assert (await inv2.invoke(tool, {"petId": 2})).ok
        assert "api_key=ak-1" in str(route2.calls.last.request.url)

    @respx.mock
    async def test_basic_auth(self, monkeypatch):
        monkeypatch.setenv("U", "user1")
        monkeypatch.setenv("P", "pass1")
        from openapi_mcp_hub.models import AuthConfig

        route = respx.get(f"{BASE}/pets/1").mock(return_value=httpx.Response(200, json={}))
        tool = _tool(params=[ApiParam("petId", ParamLocation.path, required=True)])
        inv = Invoker(
            ServiceHttpConfig(BASE, AuthConfig(type="basic", user_env="U", password_env="P"))
        )
        assert (await inv.invoke(tool, {"petId": 1})).ok
        expected = "Basic " + base64.b64encode(b"user1:pass1").decode()
        assert route.calls.last.request.headers["authorization"] == expected


class TestErrorsAndTruncation:
    @respx.mock
    async def test_http_error_mapped(self):
        respx.get(f"{BASE}/pets/999").mock(return_value=httpx.Response(404, text='{"msg": "not found"}'))
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(_tool(), {"petId": 999})
        assert not result.ok
        assert result.status_code == 404
        assert "404" in result.error and "not found" in result.error

    @respx.mock
    async def test_network_error(self):
        respx.get(f"{BASE}/pets/1").mock(side_effect=httpx.ConnectError("boom"))
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(_tool(), {"petId": 1})
        assert not result.ok and "网络错误" in result.error

    @respx.mock
    async def test_timeout(self):
        respx.get(f"{BASE}/pets/1").mock(side_effect=httpx.ReadTimeout("slow"))
        inv = Invoker(ServiceHttpConfig(BASE, None, timeout_s=1.0))
        result = await inv.invoke(_tool(), {"petId": 1})
        assert not result.ok and "超时" in result.error

    @respx.mock
    async def test_success_truncated(self):
        big = {"data": "x" * (70 * 1024)}
        respx.get(f"{BASE}/pets/1").mock(return_value=httpx.Response(200, json=big))
        inv = Invoker(ServiceHttpConfig(BASE, None))
        result = await inv.invoke(_tool(), {"petId": 1})
        assert result.ok
        assert isinstance(result.payload, str)  # 超限被截断为文本
        assert "已截断" in result.payload
