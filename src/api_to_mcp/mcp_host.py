"""MCP 宿主：每个注册服务一个独立 MCP server 实例，统一挂在 /mcp/{name} 下。

基于 mcp SDK 2.x（types 为 snake_case：input_schema / is_error）：
- lowlevel Server 构造器直接注入 on_list_tools / on_call_tool 回调；
- 2.x 不再自动校验入参，网关用 jsonschema 按 inputSchema 自行校验；
- StreamableHTTPSessionManager(stateless=True, json_response=True)：
  每个 POST 独立处理、无会话状态 => 天然支持热更新（换实例即生效）；
- security_settings=None：默认关闭 DNS rebinding 防护（网关部署在内网
  IP 上，Host 头非 localhost，开启会被误杀）；
- 数据面鉴权：X-Api-Key 与注册时落盘的 sha256 哈希比对。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from typing import Any

import jsonschema
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from . import __version__
from .invoker import Invoker
from .models import ToolDef
from .templates import render_response

logger = logging.getLogger(__name__)


class McpServiceApp:
    """单个服务的 MCP 运行实例（Server + SessionManager + 生命周期）。"""

    def __init__(
        self,
        name: str,
        title: str | None,
        tools: list[ToolDef],
        invoker: Invoker | None = None,
        *,
        allowed_tools: list[str] | None = None,
        db_executor: "DbExecutor | None" = None,
    ):
        self.name = name
        self.title = title
        self.tools = tools
        self.invoker = invoker
        self.db_executor = db_executor
        self._tool_map = {t.name: t for t in tools}
        # 运行时工具白名单（空 = 全部暴露；list/call 两层都生效）
        self._allowed: set[str] | None = set(allowed_tools) if allowed_tools else None
        # 近期调用日志（内存环形缓冲，管理面 /services/{name}/calls 查看）
        self.calls: deque[dict[str, Any]] = deque(maxlen=200)

        server = Server(
            f"api-to-mcp/{name}",
            version=__version__,
            title=title,
            description="本服务由 api-to-mcp 网关从 openapi.json 自动生成，工具调用将代理到真实 REST API。",
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

        self._server = server
        self._manager = StreamableHTTPSessionManager(
            app=server,
            stateless=True,
            json_response=True,
            security_settings=None,
        )
        # SSE 兼容端点（老客户端）：与 streamable 共享同一 Server 实例；
        # endpoint 传 /{name}/message，SDK 会拼上 Mount 前缀 root_path 后下发给客户端
        self._sse_transport: SseServerTransport | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ MCP 回调（2.x 签名 (ctx, params)）

    async def _on_list_tools(self, ctx: Any, params: Any) -> ListToolsResult:
        exposed = [t for t in self.tools if self._allowed is None or t.name in self._allowed]
        return ListToolsResult(
            tools=[
                Tool(name=t.name, description=t.description, input_schema=t.input_schema)
                for t in exposed
            ]
        )

    async def _on_call_tool(self, ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        start = time.monotonic()
        result = await self._do_call_tool(params)
        self._log_call(params.name, result, time.monotonic() - start)
        return result

    async def _do_call_tool(self, params: CallToolRequestParams) -> CallToolResult:
        tool = self._tool_map.get(params.name)
        if tool is None:
            return self._error_result(
                f"未知工具: {params.name}（服务可能刚更新，请重新获取工具列表）",
                code=-32601,
                tool=params.name,
            )
        if self._allowed is not None and tool.name not in self._allowed:
            return self._error_result(
                f"工具 {tool.name} 不在 allowed_tools 白名单内（联系服务管理员调整）",
                code=-32601,
                tool=tool.name,
            )

        arguments = params.arguments or {}
        # 2.x SDK 不做入参校验，网关按 inputSchema 校验（拦住 LLM 编造参数）
        try:
            jsonschema.validate(instance=arguments, schema=tool.input_schema)
        except jsonschema.ValidationError as exc:
            return self._error_result(
                f"Input validation error: {exc.message}", code=-32602, tool=tool.name
            )
        except jsonschema.SchemaError as exc:
            logger.warning("工具 %s 的 inputSchema 非法，跳过校验: %s", params.name, exc)
        # LLM 未传的可选参数填上 schema default（Unla fillDefaultArgs 语义）
        for pname, prop in (tool.input_schema.get("properties") or {}).items():
            if pname not in arguments and isinstance(prop, dict) and "default" in prop:
                arguments[pname] = prop["default"]

        try:
            if tool.db_action is not None:
                if self.db_executor is None:  # pragma: no cover（注册期保证成对出现）
                    return self._error_result(
                        f"工具 {tool.name} 缺少数据库执行器", code=-32603, tool=tool.name
                    )
                result = await self.db_executor.invoke(tool, arguments)
            else:
                assert self.invoker is not None
                result = await self.invoker.invoke(tool, arguments)
        except Exception as exc:  # noqa: BLE001 兜底：任何异常都以 isError 返回
            logger.exception("工具 %s 调用异常", params.name)
            return self._error_result(
                f"网关内部错误: {exc.__class__.__name__}: {exc}", code=-32603, tool=tool.name
            )

        if not result.ok:
            return self._error_result(
                result.error or "调用失败",
                code=result.error_code or -32000,
                status_code=result.status_code,
                tool=tool.name,
            )

        # 响应模板：把原始响应裁剪成 LLM 友好文本；渲染异常则回退 passthrough
        if tool.response_template:
            try:
                text = render_response(
                    tool.response_template,
                    response=result.payload,
                    status=result.status_code or 200,
                    tool=tool.name,
                )
            except Exception:  # noqa: BLE001 模板绝不拖垮调用
                logger.exception("工具 %s 响应模板渲染失败，回退原始响应", tool.name)
                text = None
            if text is not None:
                return CallToolResult(content=[TextContent(type="text", text=text)])

        text = (
            result.payload
            if isinstance(result.payload, str)
            else json.dumps(result.payload, ensure_ascii=False, default=str)
        )
        return CallToolResult(content=[TextContent(type="text", text=text)])

    _STATUS_BY_CODE = {
        -32601: "unknown_tool",
        -32602: "invalid_params",
        -32000: "downstream",
        -32001: "auth",
        -32002: "network",
        -32603: "internal",
    }

    def _log_call(self, tool_name: str, result: CallToolResult, duration: float) -> None:
        """追加一条调用日志（元数据 + 错误摘要，不含请求/响应原文）。"""
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "duration_ms": round(duration * 1000, 1),
        }
        if result.is_error:
            try:
                err = json.loads(result.content[0].text).get("error", {})
            except (json.JSONDecodeError, IndexError, AttributeError):
                err = {}
            entry.update(
                status=self._STATUS_BY_CODE.get(err.get("code"), "error"),
                error_code=err.get("code"),
                message=(err.get("message") or "")[:200],
            )
        else:
            entry["status"] = "ok"
        self.calls.append(entry)

    def _error_result(
        self,
        message: str,
        *,
        code: int = -32603,
        status_code: int | None = None,
        tool: str | None = None,
    ) -> CallToolResult:
        """结构化错误载荷（对齐 JSON-RPC 错误对象：两层错误模型）。

        code 取值：
        - -32601 未知工具（method not found 语义）
        - -32602 入参校验失败（invalid params）
        - -32603 网关内部错误
        - -32000 目标 API 返回非 2xx（data.http_status 携带原始状态码）
        - -32001 目标 API 凭证缺失/非法（env 未配置）
        - -32002 网络/超时错误
        """
        data: dict[str, Any] = {}
        if status_code is not None:
            data["http_status"] = status_code
        if tool is not None:
            data["tool"] = tool
        if self.name:
            data["service"] = self.name
        payload = {"error": {"code": code, "message": message, "data": data}}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            is_error=True,
        )

    # ------------------------------------------------------------ 生命周期

    async def _run(self) -> None:
        try:
            async with self._manager.run():
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        """启动 SessionManager（等待内部 task group 就绪后返回）。"""
        if self._task is not None and not self._task.done():
            return
        self._ready.clear()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"mcp-service:{self.name}")
        await self._ready.wait()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        if self.invoker is not None:
            await self.invoker.cfg.close()
        if self.db_executor is not None:
            await self.db_executor.close()

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    async def handle(self, scope: dict, receive, send) -> None:
        """数据面入口：path 后缀分流 streamable / sse / message。"""
        if not self.started:
            await _send_json(send, 503, {"detail": f"服务 {self.name} 正在启动/已停止"})
            return
        # 剥离 Mount 前缀（root_path）后取服务名之后的子路径
        path: str = scope.get("path", "")
        root_path: str = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path):]
        segments = [s for s in path.split("/") if s][1:]  # 跳过服务名
        sub = segments[0] if segments else ""

        if sub == "":
            await self._manager.handle_request(scope, receive, send)
        elif sub == "sse":
            await self.handle_sse(scope, receive, send)
        elif sub == "message":
            await self.handle_message(scope, receive, send)
        else:
            await _send_json(
                send,
                404,
                {"detail": f"路径格式应为 /mcp/{self.name}（或 /sse、/message 兼容端点）"},
            )

    # ------------------------------------------------------------ SSE 兼容（老客户端）

    def _ensure_sse(self) -> SseServerTransport:
        if self._sse_transport is None:
            self._sse_transport = SseServerTransport(f"/{self.name}/message")
        return self._sse_transport

    async def handle_sse(self, scope: dict, receive, send) -> None:
        """GET /mcp/{name}/sse：建立 SSE 长连接，跑同一套 Server 回调。"""
        transport = self._ensure_sse()
        options = self._server.create_initialization_options()
        async with transport.connect_sse(scope, receive, send) as streams:
            await self._server.run(streams[0], streams[1], options, raise_exceptions=False)

    async def handle_message(self, scope: dict, receive, send) -> None:
        """POST /mcp/{name}/message?session_id=...：SSE 会话的客户端上行通道。"""
        await self._ensure_sse().handle_post_message(scope, receive, send)


class McpHost:
    """ASGI 分发器：/mcp 前缀下按服务名路由到对应 McpServiceApp。"""

    def __init__(self) -> None:
        self._apps: dict[str, McpServiceApp] = {}
        self._key_hashes: dict[str, str] = {}

    # ------------------------------------------------------------ 服务管理

    async def install(self, name: str, app: McpServiceApp, api_key_hash: str) -> McpServiceApp | None:
        """挂载（同名覆盖：先挂新实例并就绪，再停旧实例）；返回被替换的旧实例。"""
        old = self._apps.get(name)
        self._apps[name] = app
        self._key_hashes[name] = api_key_hash
        await app.start()
        if old is not None:
            await old.stop()
        return old

    async def uninstall(self, name: str) -> None:
        app = self._apps.pop(name, None)
        self._key_hashes.pop(name, None)
        if app is not None:
            await app.stop()

    def rekey(self, name: str, api_key_hash: str) -> None:
        """轮换数据面 key：只替换哈希，MCP 实例无需重启。"""
        self._key_hashes[name] = api_key_hash

    async def shutdown(self) -> None:
        for name in list(self._apps):
            await self.uninstall(name)

    def get(self, name: str) -> McpServiceApp | None:
        return self._apps.get(name)

    def names(self) -> list[str]:
        return sorted(self._apps)

    # ------------------------------------------------------------ ASGI 入口

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            raise RuntimeError(f"McpHost 不支持 {scope['type']} scope")

        # 新版 Starlette Mount 不改写子应用 path，挂载前缀放在 root_path：
        # 需剥离 root_path 后再解析 /{name}[/...]，兼容直接裸挂与 Mount 两种方式
        path: str = scope.get("path", "")
        root_path: str = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path):]
        name = path.lstrip("/").split("/", 1)[0]
        if not name:
            await _send_json(send, 404, {"detail": "路径格式应为 /mcp/{service-name}"})
            return

        app = self._apps.get(name)
        if app is None:
            await _send_json(send, 404, {"detail": f"未注册的服务: {name}"})
            return

        key_hash = self._key_hashes.get(name, "")
        provided = _header(scope, "x-api-key")
        if not provided:
            await _send_json(send, 401, {"detail": f"缺少 X-Api-Key 请求头（服务 {name}）"})
            return
        digest = hashlib.sha256(provided.encode()).hexdigest()
        if not hmac.compare_digest(digest, key_hash):
            await _send_json(send, 401, {"detail": "X-Api-Key 无效"})
            return

        await app.handle(scope, receive, send)


def _header(scope: dict, lower_name: str) -> str | None:
    for k, v in scope.get("headers") or []:
        try:
            if k.decode("latin-1").lower() == lower_name:
                return v.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            continue
    return None


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
