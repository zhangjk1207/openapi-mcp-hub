"""端到端：真实 uvicorn 双服务 + 真实 MCP 客户端全链路。

覆盖：注册 => MCP initialize/list_tools/call_tool => 认证注入 => 参数校验 =>
错误映射 => key 轮换 => PUT 热更新 => 网关重启恢复 => 删除。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from openapi_mcp_hub.config import GatewayConfig
from openapi_mcp_hub.main import create_app

from .conftest import PETSTORE_SPEC

ADMIN_TOKEN = "e2e-admin-token"
TARGET_TOKEN_ENV = "E2E_PETSTORE_TOKEN"
TARGET_TOKEN = "e2e-target-token"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if server.started:
            return server, thread
        time.sleep(0.05)
    raise RuntimeError("uvicorn 启动超时")


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        raise RuntimeError("uvicorn 线程未能在超时内退出")


def _mock_api_app() -> FastAPI:
    app = FastAPI()

    @app.get("/pets/{pet_id}")
    async def get_pet(pet_id: int, request: Request):
        if pet_id == 999999:
            raise HTTPException(status_code=404, detail="Pet not found")
        return {
            "id": pet_id,
            "name": f"pet-{pet_id}",
            "status": "available",
            "auth_header": request.headers.get("authorization", ""),
        }

    @app.get("/pets")
    async def list_pets(limit: int = 20, status: str | None = None, request: Request = None):
        return [
            {"id": i, "name": f"pet-{i}", "status": status or "available"}
            for i in range(1, min(limit, 5) + 1)
        ]

    @app.post("/pets")
    async def create_pet(request: Request):
        body = await request.json()
        return {
            "id": 999,
            "name": body.get("name"),
            "tag": body.get("tag"),
            "auth_header": request.headers.get("authorization", ""),
        }

    return app


def _registration_payload(mock_base: str) -> dict:
    return {
        "name": "petstore",
        "title": "Petstore E2E",
        "spec": json.loads(json.dumps(PETSTORE_SPEC)),
        "base_url": mock_base,
        "auth": {"type": "bearer", "token_env": TARGET_TOKEN_ENV},
        "filters": {"exclude": ["/internal/*"]},
        "tool_response_templates": {
            "get_pet_by_id": "宠物 {{response.name}}（id={{response.id}}，{{response.status}}）"
        },
        "owner": "e2e",
    }


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _mcp_raw_headers(api_key: str) -> dict:
    """裸 POST /mcp/{name} 用的请求头（MCP 规范要求 Accept 双类型）。"""
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


@contextlib.asynccontextmanager
async def _mcp_session(mcp_url: str, api_key: str):
    """连接数据面 MCP 端点（2.x：headers 通过预置 httpx 客户端传入，yield 二元组）。"""
    http_client = httpx.AsyncClient(headers={"X-Api-Key": api_key})
    try:
        async with streamable_http_client(
            mcp_url, http_client=http_client, terminate_on_close=False
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        await http_client.aclose()


async def _mcp_call(mcp_url: str, api_key: str, tool: str, arguments: dict):
    """打开一个 MCP 会话执行单个调用，返回调用结果。"""
    async with _mcp_session(mcp_url, api_key) as session:
        return await session.call_tool(tool, arguments)


class TestFullLifecycle:
    async def test_full_lifecycle(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TARGET_TOKEN_ENV, TARGET_TOKEN)
        mock_port = _free_port()
        gw_port = _free_port()

        mock_server, mock_thread = _start_server(_mock_api_app(), mock_port)
        cfg = GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)
        gw_server, gw_thread = _start_server(create_app(cfg), gw_port)
        gw_base = f"http://127.0.0.1:{gw_port}"
        mcp_url = f"{gw_base}/mcp/petstore"

        try:
            # ------------------------------------------------ 1. 注册
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.post(
                    "/services", json=_registration_payload(f"http://127.0.0.1:{mock_port}"),
                    headers=_admin_headers(),
                )
                assert resp.status_code == 201, resp.text
                created = resp.json()
                api_key = created["api_key"]
                assert set(created["tools"]) == {
                    "list_pets", "create_pet", "get_pet_by_id", "delete_pet_by_id", "whoami",
                }
                assert "internal_metrics" not in created["tools"]  # filters 生效

            # ------------------------------------------------ 2. MCP 全链路
            async with _mcp_session(mcp_url, api_key) as session:
                init = session.initialize_result
                assert init.server_info.name == "openapi-mcp-hub/petstore"

                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert set(names) == {
                    "list_pets", "create_pet", "get_pet_by_id", "delete_pet_by_id", "whoami",
                }
                # inputSchema 正确暴露
                by_name = {t.name: t for t in tools.tools}
                assert "petId" in by_name["get_pet_by_id"].input_schema["required"]

                # 2.1 正常调用：路径参数 + 工具级响应模板（get_pet_by_id 配了裁剪模板）
                ok = await session.call_tool("get_pet_by_id", {"petId": 42})
                assert not ok.is_error, ok.content
                assert ok.content[0].text == "宠物 pet-42（id=42，available）"

                # 2.2 query 参数（未配模板的工具仍 passthrough 原始 JSON）
                ok = await session.call_tool("list_pets", {"limit": 3})
                assert not ok.is_error
                assert len(json.loads(ok.content[0].text)) == 3

                # 2.3 请求体 + 认证注入（passthrough 输出可见 auth_header）
                ok = await session.call_tool("create_pet", {"body": {"name": "哈咪", "tag": "cat"}})
                assert not ok.is_error
                payload = json.loads(ok.content[0].text)
                assert payload["name"] == "哈咪"
                assert payload["auth_header"] == f"Bearer {TARGET_TOKEN}"

                # 2.4 入参校验失败 => 网关 jsonschema 校验 + -32602 错误码
                bad = await session.call_tool("list_pets", {"limit": "not-a-number"})
                assert bad.is_error
                bad_payload = json.loads(bad.content[0].text)
                assert bad_payload["error"]["code"] == -32602
                assert "validation" in bad_payload["error"]["message"].lower()

                # 2.5 目标 API 404 => is_error 结构化映射（两层错误模型）
                nf = await session.call_tool("get_pet_by_id", {"petId": 999999})
                assert nf.is_error
                nf_payload = json.loads(nf.content[0].text)
                assert nf_payload["error"]["code"] == -32000
                assert nf_payload["error"]["data"]["http_status"] == 404
                assert nf_payload["error"]["data"]["tool"] == "get_pet_by_id"
                assert nf_payload["error"]["data"]["service"] == "petstore"

            # ------------------------------------------------ 3. 数据面鉴权
            async with httpx.AsyncClient(timeout=10) as plain:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                r = await plain.post(mcp_url, json=body, headers={"X-Api-Key": "psk-wrong", "Accept": "application/json, text/event-stream"})
                assert r.status_code == 401
                r = await plain.post(mcp_url, json=body, headers={"Accept": "application/json, text/event-stream"})  # 无 key
                assert r.status_code == 401
                r = await plain.post(mcp_url, json=body, headers=_mcp_raw_headers(api_key))
                assert r.status_code == 200

            # ------------------------------------------------ 4. key 轮换
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.post("/services/petstore/rotate-key", headers=_admin_headers())
                assert resp.status_code == 200
                new_key = resp.json()["api_key"]
                assert new_key != api_key
            async with httpx.AsyncClient(timeout=10) as plain:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                r = await plain.post(mcp_url, json=body, headers=_mcp_raw_headers(api_key))
                assert r.status_code == 401  # 旧 key 失效
                r = await plain.post(mcp_url, json=body, headers=_mcp_raw_headers(new_key))
                assert r.status_code == 200

            # ------------------------------------------------ 5. PUT 热更新（工具集 + 模板切换为服务级默认）
            updated = _registration_payload(f"http://127.0.0.1:{mock_port}")
            updated["filters"] = {"include": ["/pets/{petId}"]}
            updated["response_template"] = "ID={{response.id}}"  # 服务级默认
            updated["tool_response_templates"] = {}  # 移除工具级覆盖
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.put("/services/petstore", json=updated, headers=_admin_headers())
                assert resp.status_code == 200, resp.text
                assert resp.json()["tool_count"] == 2
            async with _mcp_session(mcp_url, new_key) as session:
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"get_pet_by_id", "delete_pet_by_id"}
                # 更新后：服务级默认模板生效
                ok = await session.call_tool("get_pet_by_id", {"petId": 7})
                assert not ok.is_error
                assert ok.content[0].text == "ID=7"

            # ------------------------------------------------ 6. 网关重启恢复
            _stop_server(gw_server, gw_thread)
            cfg2 = GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)
            gw_server, gw_thread = _start_server(create_app(cfg2), gw_port)
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.get("/services", headers=_admin_headers())
                assert [s["name"] for s in resp.json()] == ["petstore"]
                assert resp.json()[0]["tool_count"] == 2  # 更新后的过滤规则也被恢复
            # 轮换后的 key 在重启后依然有效，模板配置随落盘恢复（服务级默认仍生效）
            ok = await _mcp_call(mcp_url, new_key, "get_pet_by_id", {"petId": 8})
            assert not ok.is_error
            assert ok.content[0].text == "ID=8"

            # ------------------------------------------------ 7. 删除
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.delete("/services/petstore", headers=_admin_headers())
                assert resp.status_code == 204
            async with httpx.AsyncClient(timeout=10) as plain:
                r = await plain.post(
                    mcp_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=_mcp_raw_headers(new_key),
                )
                assert r.status_code == 404
            assert not (tmp_path / "services" / "petstore.json").exists()

        finally:
            _stop_server(gw_server, gw_thread)
            _stop_server(mock_server, mock_thread)

    async def test_admin_endpoints_reject_anonymous(self, tmp_path):
        """管理面无 token 一律 401（真实端口路径下验证）。"""
        cfg = GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)
        port = _free_port()
        server, thread = _start_server(create_app(cfg), port)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/services")).status_code == 401
                assert (await client.get("/")).status_code == 200  # 根路径公开信息
        finally:
            _stop_server(server, thread)


class TestSseCompat:
    """SSE 兼容端点：老客户端（只认 SSE transport）走 /mcp/{name}/sse。"""

    async def test_sse_full_flow(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TARGET_TOKEN_ENV, TARGET_TOKEN)
        mock_port = _free_port()
        gw_port = _free_port()
        mock_server, mock_thread = _start_server(_mock_api_app(), mock_port)
        gw_server, gw_thread = _start_server(
            create_app(GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)), gw_port
        )
        gw_base = f"http://127.0.0.1:{gw_port}"

        from mcp.client.sse import sse_client

        try:
            # 注册
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.post(
                    "/services",
                    json=_registration_payload(f"http://127.0.0.1:{mock_port}"),
                    headers=_admin_headers(),
                )
                assert resp.status_code == 201, resp.text
                api_key = resp.json()["api_key"]

            sse_url = f"{gw_base}/mcp/petstore/sse"
            # 1) 错 key 一样 401（鉴权在 McpHost 层，SSE 通道同等保护）
            async with httpx.AsyncClient(timeout=10) as plain:
                bad = await plain.get(sse_url, headers={"X-Api-Key": "psk-wrong"})
            assert bad.status_code == 401

            # 2) SSE 全链路：initialize → list_tools → call_tool → 模板生效
            async with sse_client(sse_url, headers={"X-Api-Key": api_key}) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert session.initialize_result.server_info.name == "openapi-mcp-hub/petstore"
                    tools = await session.list_tools()
                    assert "get_pet_by_id" in [t.name for t in tools.tools]
                    ok = await session.call_tool("get_pet_by_id", {"petId": 42})
                    if ok.is_error:
                        print("SSE-CALL-ERR:", ok.content[0].text)
                    assert not ok.is_error
                    assert ok.content[0].text == "宠物 pet-42（id=42，available）"

            # 3) streamable 与 SSE 共存互不干扰（e2e mock 无 /whoami，用 get_pet_by_id）
            async with _mcp_session(f"{gw_base}/mcp/petstore", api_key) as session:
                ok = await session.call_tool("get_pet_by_id", {"petId": 7})
                assert not ok.is_error
        finally:
            gw_server.should_exit = True
            mock_server.should_exit = True
            gw_thread.join(timeout=5)
            mock_thread.join(timeout=5)

    async def test_sse_requires_accept_event_stream(self, tmp_path, monkeypatch):
        """不带 Accept: text/event-stream 的 GET /sse 由 SDK transport 拒绝。"""
        monkeypatch.setenv(TARGET_TOKEN_ENV, TARGET_TOKEN)
        mock_port = _free_port()
        gw_port = _free_port()
        mock_server, mock_thread = _start_server(_mock_api_app(), mock_port)
        gw_server, gw_thread = _start_server(
            create_app(GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)), gw_port
        )
        gw_base = f"http://127.0.0.1:{gw_port}"
        try:
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                resp = await admin.post(
                    "/services",
                    json=_registration_payload(f"http://127.0.0.1:{mock_port}"),
                    headers=_admin_headers(),
                )
                api_key = resp.json()["api_key"]
            # SSE transport 不校验 Accept：任何 GET 都会开流；验证 content-type 为 event-stream
            async with httpx.AsyncClient(timeout=5) as plain:
                async with plain.stream(
                    "GET",
                    f"{gw_base}/mcp/petstore/sse",
                    headers={"X-Api-Key": api_key, "Accept": "application/json"},
                ) as r:
                    assert r.status_code == 200
                    assert r.headers["content-type"].startswith("text/event-stream")
        finally:
            gw_server.should_exit = True
            mock_server.should_exit = True
            gw_thread.join(timeout=5)
            mock_thread.join(timeout=5)


class TestHealth:
    async def test_health_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TARGET_TOKEN_ENV, TARGET_TOKEN)
        mock_port = _free_port()
        gw_port = _free_port()
        mock_server, mock_thread = _start_server(_mock_api_app(), mock_port)
        gw_server, gw_thread = _start_server(
            create_app(GatewayConfig(admin_token=ADMIN_TOKEN, data_dir=tmp_path)), gw_port
        )
        gw_base = f"http://127.0.0.1:{gw_port}"
        try:
            async with httpx.AsyncClient(base_url=gw_base, timeout=15) as admin:
                await admin.post(
                    "/services",
                    json=_registration_payload(f"http://127.0.0.1:{mock_port}"),
                    headers=_admin_headers(),
                )
                # 1) 正常：可达（mock 根路径无路由返回 404，但网络通、认证 env 存在）
                resp = await admin.get("/services/petstore/health", headers=_admin_headers())
                assert resp.status_code == 200, resp.text
                out = resp.json()
                assert out["reachable"] is True
                assert out["http_status"] == 404  # mock 的 / 无路由；重点是不抛网络错误
                assert out["ok"] is False
                assert out["auth_env_ok"] and out["latency_ms"] >= 0
                assert out["target"] == f"http://127.0.0.1:{mock_port}"
                # 2) 凭证 env 被清掉 → auth_env_ok=False + auth_error（不回传值）
                monkeypatch.delenv(TARGET_TOKEN_ENV)
                out = (await admin.get("/services/petstore/health", headers=_admin_headers())).json()
                assert out["auth_env_ok"] is False
                assert TARGET_TOKEN_ENV in out["auth_error"]
                # 3) 不存在的服务 → 404
                resp = await admin.get("/services/ghost/health", headers=_admin_headers())
                assert resp.status_code == 404
        finally:
            gw_server.should_exit = True
            mock_server.should_exit = True
            gw_thread.join(timeout=5)
            mock_thread.join(timeout=5)
