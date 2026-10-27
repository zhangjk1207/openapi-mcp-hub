"""stdio ↔ HTTP 桥：把远端网关的一个 MCP 端点暴露成本地 stdio server。

为什么需要这个：官方 MCP Registry 的 PyPI 包只允许 `transport: stdio`，而本网关是常驻
HTTP 服务；另外 Claude Desktop 之类只支持 stdio 的客户端也进不来。桥只做协议搬运——
不解析业务语义、不缓存、不改写，所有请求原样转发给 `--url` 指向的端点。

设计取舍（都是为了避免"桥自己变成第二套实现"）：

- 不做 server→client 请求（sampling / elicitation / progress）：网关不发起，故不实现。
- 不改写响应：客户端拿到的就是网关返回的 JSON-RPC。
- 只补两类必要信息：`Mcp-Session-Id`（会话粘滞）与 `MCP-Protocol-Version` / `Mcp-Method`
  / `Mcp-Name`（2026-07-28 协议要求的请求头，由客户端请求内容推导）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

import httpx

# stdio 帧格式：一行一个 JSON-RPC 消息（MCP 规范）
_DEFAULT_TIMEOUT = 60.0


def _parse_header_args(pairs: list[str] | None) -> dict[str, str]:
    """把 `--header K=V` 解析成 dict；重复键后者覆盖前者。"""
    headers: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit(f"--header 需要 K=V 形式，收到：{raw!r}")
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"--header 的键不能为空：{raw!r}")
        headers[key] = value.strip()
    return headers


def _request_headers(message: Any) -> dict[str, str]:
    """从客户端请求推导 2026-07-28 协议要求的头（推不出就不发，交给网关判断）。"""
    if not isinstance(message, dict):
        return {}
    out: dict[str, str] = {}
    method = message.get("method")
    if isinstance(method, str):
        out["Mcp-Method"] = method
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict):
        version = meta.get("io.modelcontextprotocol/protocolVersion")
        if isinstance(version, str):
            out["MCP-Protocol-Version"] = version
    if method == "tools/call" and isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            out["Mcp-Name"] = name
    return out


def _parse_sse_payloads(text: str) -> list[Any]:
    """从 SSE 响应体里取出所有 data 行拼成的事件负载。

    streamable HTTP 在需要推流时会返回 text/event-stream；对本桥来说一次请求
    只会等到一条对应 id 的响应，其余（注释心跳、其它通知）丢弃即可。
    """
    payloads: list[Any] = []
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if not line.strip() and data_lines:
            payloads.append(_loads("\n".join(data_lines)))
            data_lines = []
    if data_lines:  # 结尾没有空行也要收下
        payloads.append(_loads("\n".join(data_lines)))
    return payloads


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _pick_response(payloads: list[Any], message_id: Any) -> Any | None:
    """选出属于本请求的响应：优先 id 匹配，否则取第一个带 result/error 的。"""
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("id") == message_id:
            return payload
    for payload in payloads:
        if isinstance(payload, dict) and ("result" in payload or "error" in payload):
            return payload
    return None


class Bridge:
    """一个客户端会话对应一个 Bridge 实例（持有 session id 与连接池）。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url
        # Content-Type 必须显式声明：streamable HTTP 要求双向 JSON，缺了网关直接 400
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self.timeout = timeout
        self.session_id: str | None = None
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def forward(self, message: Any) -> Any | None:
        """转发一条消息，返回应写回 stdout 的响应（通知则返回 None）。"""
        headers = {**self.headers, **_request_headers(message)}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = self._client.post(
                self.url,
                headers=headers,
                content=json.dumps(message, ensure_ascii=False).encode(),
            )
        except httpx.HTTPError as exc:
            return _transport_error(message, f"无法连接网关 {self.url}：{exc}")

        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid

        if resp.status_code >= 400:
            # 网关用 HTTP 状态码表达鉴权/限流等失败，翻译成 JSON-RPC 错误交还客户端
            return _transport_error(
                message, f"网关返回 HTTP {resp.status_code}：{resp.text[:300]}", resp.status_code
            )

        content_type = resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            picked = _pick_response(_parse_sse_payloads(resp.text), _message_id(message))
            if picked is not None:
                return picked
            return None if not _expects_response(message) else _transport_error(
                message, "网关未返回对应响应（SSE 流里没有匹配 id 的消息）"
            )

        if not resp.content:
            return None  # 通知的 202 Accepted
        parsed = _loads(resp.text)
        if isinstance(parsed, list):  # 批量请求
            return parsed
        if parsed is None:
            return _transport_error(message, f"网关响应不是合法 JSON：{resp.text[:300]}")
        return parsed


def _message_id(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("id")
    if isinstance(message, list) and message and isinstance(message[0], dict):
        return message[0].get("id")
    return None


def _expects_response(message: Any) -> bool:
    """JSON-RPC：没有 id 的是通知，不需要响应。"""
    if isinstance(message, dict):
        return "id" in message
    return True


def _transport_error(message: Any, detail: str, http_status: int | None = None) -> Any:
    message_id = _message_id(message)
    if http_status == 401:
        hint = "鉴权失败：检查 --header 里的 X-Api-Key 是否正确。"
    elif http_status == 404:
        hint = "端点不存在：确认 --url 指向 /mcp/<service>，且服务已注册。"
    else:
        hint = "检查网关是否在运行、--url 是否可达。"
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": -32000,
            "message": f"{detail}（{hint}）",
            "data": {"hint": hint, "transport": "stdio-bridge"},
        },
    }


def serve(url: str, headers: dict[str, str] | None = None, timeout: float = _DEFAULT_TIMEOUT,
          stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """从 stdin 逐行读 JSON-RPC，转发后逐行写回 stdout。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    bridge = Bridge(url, headers=headers, timeout=timeout)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            message = _loads(line)
            if message is None:
                _write(stdout, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "桥收到非法 JSON"},
                })
                continue
            response = bridge.forward(message)
            if response is not None:
                _write(stdout, response)
    except KeyboardInterrupt:
        return 0
    finally:
        bridge.close()
    return 0


def _write(stdout: TextIO, payload: Any) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openapi-mcp-hub bridge",
        description="stdio ↔ HTTP 桥：把远端网关的 MCP 端点暴露成本地 stdio server",
    )
    p.add_argument(
        "--url", required=True,
        help="网关的 MCP 端点，例如 http://localhost:20033/mcp/petstore",
    )
    p.add_argument(
        "--header", action="append", metavar="K=V",
        help="随请求发送的头（可重复），例如 --header X-Api-Key=psk-...",
    )
    p.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT,
                   help="单次请求超时秒数（默认 60）")
    return p


def run_cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(args.url, headers=_parse_header_args(args.header), timeout=args.timeout)


if __name__ == "__main__":  # python -m openapi_mcp_hub.bridge
    raise SystemExit(run_cli())
