"""invoker：HTTP 还原、认证注入、错误映射、截断（respx mock）。"""

import base64
import json

import httpx
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


class TestOAuth2:
    """oauth2 client_credentials：token 代取、缓存、401 刷新重试、错误映射。"""

    def _auth(self, token_url: str = "http://idp.test/token"):
        from openapi_mcp_hub.models import AuthConfig

        return AuthConfig(
            type="oauth2",
            token_url=token_url,
            client_id_env="OMH_CID",
            client_secret_env="OMH_CSEC",
        )

    @respx.mock
    async def test_token_fetch_cached_and_injected(self, monkeypatch):
        monkeypatch.setenv("OMH_CID", "client-id")
        monkeypatch.setenv("OMH_CSEC", "client-secret")
        token_route = respx.post("http://idp.test/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t1", "expires_in": 3600})
        )
        route = respx.get(f"{BASE}/pets/42").mock(return_value=httpx.Response(200, json={"id": 42}))
        inv = Invoker(ServiceHttpConfig(BASE, self._auth()))
        for _ in range(2):
            result = await inv.invoke(_tool(), {"petId": 42})
            assert result.ok
        # 两次调用只取一次 token（进程内缓存）
        assert token_route.calls.call_count == 1
        assert route.calls.last.request.headers["Authorization"] == "Bearer t1"
        # token 请求：client_credentials + 凭证来自环境变量
        form = token_route.calls.last.request.content.decode()
        assert "grant_type=client_credentials" in form
        assert "client_id=client-id" in form

    @respx.mock
    async def test_env_missing_returns_auth_error(self, monkeypatch):
        monkeypatch.delenv("OMH_CID", raising=False)
        monkeypatch.delenv("OMH_CSEC", raising=False)
        inv = Invoker(ServiceHttpConfig(BASE, self._auth()))
        result = await inv.invoke(_tool(), {"petId": 42})
        assert not result.ok
        assert result.error_code == -32001
        assert "OMH_CID" in result.error

    @respx.mock
    async def test_401_triggers_refresh_and_retry_once(self, monkeypatch):
        monkeypatch.setenv("OMH_CID", "client-id")
        monkeypatch.setenv("OMH_CSEC", "client-secret")
        token_route = respx.post("http://idp.test/token").mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "t1", "expires_in": 3600}),
                httpx.Response(200, json={"access_token": "t2", "expires_in": 3600}),
            ]
        )
        target = respx.get(f"{BASE}/pets/42").mock(
            side_effect=[
                httpx.Response(401, json={"detail": "token expired"}),
                httpx.Response(200, json={"id": 42}),
            ]
        )
        inv = Invoker(ServiceHttpConfig(BASE, self._auth()))
        result = await inv.invoke(_tool(), {"petId": 42})
        assert result.ok and result.payload == {"id": 42}
        # token 强制刷新了一次，两次请求分别带新旧 token
        assert token_route.calls.call_count == 2
        auths = [c.request.headers["Authorization"] for c in target.calls]
        assert auths == ["Bearer t1", "Bearer t2"]

    @respx.mock
    async def test_static_bearer_401_does_not_retry(self, monkeypatch):
        monkeypatch.setenv("STATIC_TOKEN", "static-token")
        from openapi_mcp_hub.models import AuthConfig

        route = respx.get(f"{BASE}/pets/42").mock(return_value=httpx.Response(401, json={}))
        inv = Invoker(
            ServiceHttpConfig(BASE, AuthConfig(type="bearer", token_env="STATIC_TOKEN"))
        )
        result = await inv.invoke(_tool(), {"petId": 42})
        assert not result.ok and result.status_code == 401
        assert route.calls.call_count == 1  # 静态凭证无旋转，重试无意义

    @respx.mock
    async def test_token_endpoint_error_maps_to_auth_error(self, monkeypatch):
        monkeypatch.setenv("OMH_CID", "client-id")
        monkeypatch.setenv("OMH_CSEC", "client-secret")
        respx.post("http://idp.test/token").mock(
            return_value=httpx.Response(500, text="idp down")
        )
        inv = Invoker(ServiceHttpConfig(BASE, self._auth()))
        result = await inv.invoke(_tool(), {"petId": 42})
        assert not result.ok and result.error_code == -32001
        assert "HTTP 500" in result.error

    @respx.mock
    async def test_scope_from_env_and_literal(self, monkeypatch):
        monkeypatch.setenv("OMH_CID", "client-id")
        monkeypatch.setenv("OMH_CSEC", "client-secret")
        monkeypatch.setenv("OMH_SCOPE", "read:pets write:pets")
        respx.get(f"{BASE}/pets/42").mock(return_value=httpx.Response(200, json={"id": 42}))
        # scope_env 优先
        token_route = respx.post("http://idp.test/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t1", "expires_in": 3600})
        )
        from openapi_mcp_hub.models import AuthConfig

        inv = Invoker(ServiceHttpConfig(BASE, AuthConfig(
            type="oauth2", token_url="http://idp.test/token",
            client_id_env="OMH_CID", client_secret_env="OMH_CSEC",
            scope="ignored-literal", scope_env="OMH_SCOPE",
        )))
        assert (await inv.invoke(_tool(), {"petId": 42})).ok
        form = token_route.calls.last.request.content.decode()
        assert "scope=read%3Apets+write%3Apets" in form
        inv2 = Invoker(ServiceHttpConfig(BASE, AuthConfig(
            type="oauth2", token_url="http://idp.test/token",
            client_id_env="OMH_CID", client_secret_env="OMH_CSEC",
            scope="literal-scope",
        )))
        assert (await inv2.invoke(_tool(), {"petId": 42})).ok
        form2 = token_route.calls.last.request.content.decode()
        assert "scope=literal-scope" in form2
