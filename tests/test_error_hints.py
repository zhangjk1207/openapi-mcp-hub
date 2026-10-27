"""错误提示（hint）：让 agent 能自己改对，而不是盲目重试。

覆盖两层：
1. invoker.status_hint：按上游状态码给出"改什么/是否值得重试"的结论；
2. 端到端：错误载荷里必须带 data.hint（MCP 客户端可直接读）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from openapi_mcp_hub.invoker import Invoker, ServiceHttpConfig, status_hint
from openapi_mcp_hub.models import ApiEndpoint, ApiParam, ParamLocation, ToolDef

BASE = "http://target.test"


@pytest.mark.parametrize(
    "code,fragment",
    [
        (400, "inputSchema"),
        (401, "重试不会成功"),
        (403, "重试无效"),
        (404, "重试无效"),
        (409, "先"),
        (422, "字段"),
        (429, "限流"),
        (500, "稍后重试"),
        (503, "等待"),
    ],
)
def test_status_hint_is_specific(code, fragment):
    assert fragment in status_hint(code)


def test_status_hint_fallbacks():
    assert "4" not in status_hint(418)  # 不泄露内部编号，给的是行动建议
    assert "修正参数" in status_hint(418)
    assert "上游服务端错误" in status_hint(599)
    assert "非预期" in status_hint(302)


def _tool() -> ToolDef:
    ep = ApiEndpoint(
        method="get",
        path="/pets/{petId}",
        operation_id="get_pet",
        params=[ApiParam("petId", ParamLocation.path, required=True, schema={"type": "integer"})],
    )
    return ToolDef(name="get_pet", description="", input_schema={}, endpoint=ep)


@respx.mock
async def test_upstream_429_carries_retry_hint():
    respx.get(f"{BASE}/pets/1").mock(
        return_value=httpx.Response(429, text="slow down", headers={"Retry-After": "3"})
    )
    result = await Invoker(ServiceHttpConfig(BASE, None)).invoke(_tool(), {"petId": 1})
    assert not result.ok
    assert result.status_code == 429
    assert result.hint and "限流" in result.hint


@respx.mock
async def test_upstream_500_hint_says_retry_once():
    respx.get(f"{BASE}/pets/1").mock(return_value=httpx.Response(500, text="boom"))
    result = await Invoker(ServiceHttpConfig(BASE, None)).invoke(_tool(), {"petId": 1})
    assert result.hint and "稍后重试" in result.hint


@respx.mock
async def test_timeout_hint_suggests_smaller_page():
    respx.get(f"{BASE}/pets/1").mock(side_effect=httpx.ReadTimeout("t"))
    result = await Invoker(ServiceHttpConfig(BASE, None)).invoke(_tool(), {"petId": 1})
    assert result.hint and "limit" in result.hint


async def test_missing_path_param_hint():
    result = await Invoker(ServiceHttpConfig(BASE, None)).invoke(_tool(), {})
    assert result.error_code == -32602
    assert result.hint and "path 参数" in result.hint


# ---------------------------------------------------------------- 端到端


async def test_tool_call_error_payload_has_hint(gateway, petstore_spec):
    reg = await gateway.post(
        "/services",
        json={"name": "hint-svc", "spec": petstore_spec, "base_url": "http://target.test"},
    )
    assert reg.status_code == 201
    key = reg.json()["api_key"]

    with respx.mock:
        respx.get("http://target.test/pets/1").mock(return_value=httpx.Response(404))
        resp = await gateway.post(
            "/mcp/hint-svc",
            headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_pet_by_id", "arguments": {"petId": 1}},
            },
        )
    body = resp.json()["result"]
    assert body["isError"] is True
    payload = __import__("json").loads(body["content"][0]["text"])
    assert payload["error"]["data"]["hint"], "错误载荷必须带 hint"
    assert payload["error"]["data"]["http_status"] == 404

    await gateway.delete("/services/hint-svc")


async def test_validation_error_hint_lists_params(gateway, petstore_spec):
    reg = await gateway.post(
        "/services",
        json={"name": "hint-val", "spec": petstore_spec, "base_url": "http://target.test"},
    )
    assert reg.status_code == 201
    key = reg.json()["api_key"]

    resp = await gateway.post(
        "/mcp/hint-val",
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_pets", "arguments": {"limit": "not-an-int"}},
        },
    )
    body = resp.json()["result"]
    assert body["isError"] is True
    payload = __import__("json").loads(body["content"][0]["text"])
    hint = payload["error"]["data"]["hint"]
    assert "可用参数" in hint
    assert "limit" in hint

    await gateway.delete("/services/hint-val")


async def test_unknown_tool_hint_tells_agent_to_refresh(gateway, petstore_spec):
    reg = await gateway.post(
        "/services",
        json={"name": "hint-unk", "spec": petstore_spec, "base_url": "http://target.test"},
    )
    key = reg.json()["api_key"]
    resp = await gateway.post(
        "/mcp/hint-unk",
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        },
    )
    payload = __import__("json").loads(resp.json()["result"]["content"][0]["text"])
    assert "tools/list" in payload["error"]["data"]["hint"]
    await gateway.delete("/services/hint-unk")
