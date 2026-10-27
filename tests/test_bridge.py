"""stdio 桥：解析单元测试 + 真实网关端到端（子进程读写 stdin/stdout）。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from openapi_mcp_hub.bridge import (
    Bridge,
    _parse_header_args,
    _parse_sse_payloads,
    _pick_response,
    _request_headers,
    serve,
)

from .conftest import PETSTORE_SPEC

# ---------------------------------------------------------------- 参数与头推导


def test_parse_header_args_allows_repeated_and_values_with_equals():
    got = _parse_header_args(["X-Api-Key=psk-a", "X-Trace=1=2"])
    assert got == {"X-Api-Key": "psk-a", "X-Trace": "1=2"}


def test_parse_header_args_rejects_malformed():
    with pytest.raises(SystemExit):
        _parse_header_args(["nope"])


def test_request_headers_from_modern_envelope():
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "list_pets",
            "arguments": {},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = _request_headers(msg)
    assert headers["Mcp-Method"] == "tools/call"
    assert headers["Mcp-Name"] == "list_pets"
    assert headers["MCP-Protocol-Version"] == "2026-07-28"


def test_request_headers_empty_for_legacy_message():
    assert _request_headers({"jsonrpc": "2.0", "id": 1, "method": "ping"}) == {
        "Mcp-Method": "ping"
    }


# ---------------------------------------------------------------- SSE 解析


def test_parse_sse_payloads_multiple_events():
    body = 'event: message\ndata: {"id":1,"result":{}}\n\ndata: {"method":"x"}\n\n'
    assert _parse_sse_payloads(body) == [{"id": 1, "result": {}}, {"method": "x"}]


def test_parse_sse_payloads_ignores_non_data_lines():
    body = ': keep-alive\n\nid: 7\ndata: {"id":2,"result":{}}\n\n'
    assert _parse_sse_payloads(body) == [{"id": 2, "result": {}}]


def test_parse_sse_payloads_without_trailing_blank_line():
    assert _parse_sse_payloads('data: {"id":3}') == [{"id": 3}]


def test_pick_response_prefers_matching_id():
    payloads = [{"method": "notifications/progress"}, {"id": 9, "result": {"a": 1}}]
    assert _pick_response(payloads, 9)["id"] == 9


def test_pick_response_falls_back_to_any_result():
    payloads = [{"id": 1, "result": {}}, {"id": 9, "result": {"a": 1}}]
    assert _pick_response(payloads, 9)["id"] == 9  # 有精确匹配就用精确的
    assert _pick_response([{"id": 5, "result": {}}], 9)["id"] == 5


# ---------------------------------------------------------------- 转发失败语义


class _FailingClient:
    def __init__(self, status: int = 401, text: str = "unauthorized") -> None:
        self.status = status
        self.text = text

    def post(self, *args, **kwargs):
        response = type(
            "Resp",
            (),
            {
                "status_code": self.status,
                "text": self.text,
                "content": b"x",
                "headers": {},
            },
        )()
        return response

    def close(self):
        pass


def test_forward_maps_http_401_to_jsonrpc_error_with_hint():
    bridge = Bridge("http://gw/mcp/petstore")
    bridge._client = _FailingClient(401)  # noqa: SLF001 - 测试替身
    result = bridge.forward({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert result["id"] == 7
    assert result["error"]["code"] == -32000
    assert "X-Api-Key" in result["error"]["data"]["hint"]


# ---------------------------------------------------------------- 端到端


@pytest.fixture
def live_gateway(tmp_path):
    """真实 uvicorn 网关 + 一个已注册的 petstore 服务。"""
    import httpx

    from openapi_mcp_hub.config import GatewayConfig
    from openapi_mcp_hub.main import create_app

    from .test_e2e import _free_port, _mock_api_app, _start_server, _stop_server

    mock_port = _free_port()
    mock_server, mock_thread = _start_server(_mock_api_app(), mock_port)
    port = _free_port()
    cfg = GatewayConfig(admin_token="adm", data_dir=tmp_path)
    app = create_app(cfg)
    server, thread = _start_server(app, port)
    base = f"http://127.0.0.1:{port}"
    try:
        resp = httpx.post(
            f"{base}/services",
            headers={"Authorization": "Bearer adm"},
            json={
                "name": "petstore",
                "spec": PETSTORE_SPEC,
                "base_url": f"http://127.0.0.1:{mock_port}",
                "target_auth": {"type": "none"},
            },
            timeout=15,
        )
        assert resp.status_code == 201, resp.text
        key = resp.json()["api_key"]
        yield f"{base}/mcp/petstore", key
    finally:
        _stop_server(server, thread)
        _stop_server(mock_server, mock_thread)


def _run_bridge(url: str, key: str, messages: list[dict]) -> list[dict]:
    """起一个真实 bridge 子进程，喂三条消息，收回响应。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "openapi_mcp_hub.bridge", "--url", url,
         "--header", f"X-Api-Key={key}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    try:
        out, err = proc.communicate(payload, timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_bridge_end_to_end_tools_list_and_call(live_gateway):
    url, key = live_gateway
    responses = _run_bridge(
        url,
        key,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "bridge-test", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "list_pets", "arguments": {"limit": 2}}},
        ],
    )
    by_id = {r["id"]: r for r in responses}
    assert by_id[1]["result"]["serverInfo"]["name"]
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert "list_pets" in names
    assert by_id[3]["result"]["isError"] is False
    assert by_id[3]["result"]["content"]  # 真实 tool 调用结果经桥原样返回


def test_bridge_reports_bad_key_as_actionable_error(live_gateway):
    url, _key = live_gateway
    responses = _run_bridge(url, "wrong-key", [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ])
    err = responses[0]["error"]
    assert err["code"] == -32000
    assert "X-Api-Key" in err["data"]["hint"]


def test_serve_handles_garbage_line_without_dying():
    """stdin 混进非 JSON 时回一条 -32700，而不是崩掉整条管道。"""
    stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    stdout = io.StringIO()

    class _OkClient:
        def post(self, *a, **kw):
            return type("R", (), {"status_code": 200, "text": '{"jsonrpc":"2.0","id":1,"result":{}}',
                                  "content": b"{}", "headers": {}})()

        def close(self):
            pass

    import openapi_mcp_hub.bridge as bridge_mod

    original = bridge_mod.Bridge.__init__

    def patched(self, *a, **kw):
        original(self, *a, **kw)
        self._client = _OkClient()

    bridge_mod.Bridge.__init__ = patched
    try:
        code = serve("http://gw/mcp/x", stdin=stdin, stdout=stdout)
    finally:
        bridge_mod.Bridge.__init__ = original

    assert code == 0
    lines = [json.loads(x) for x in stdout.getvalue().splitlines() if x.strip()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}
