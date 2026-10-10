"""Invoker：把 MCP 工具调用还原为真实 HTTP 请求。

设计要点（与 README v2 对齐）：
- per-service 独立 httpx client（独立超时/连接池）；
- 认证凭证只从环境变量读取，绝不落盘；
- 非 2xx / 网络异常 => isError 结构化结果；
- 成功响应超限截断、错误响应体摘要 4KB。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .models import AuthConfig, ParamLocation, ToolDef

SUCCESS_TRUNCATE_BYTES = 64 * 1024
ERROR_BODY_EXCERPT_BYTES = 4 * 1024


@dataclass
class InvokeResult:
    ok: bool
    status_code: int | None = None
    payload: Any = None  # ok=True 时：解析后的 JSON 或文本
    error: str | None = None  # ok=False 时：人可读错误说明
    # 错误分类码（JSON-RPC 服务器自定义区间）：
    # -32000 目标 API 非 2xx / -32001 凭证缺失 / -32002 网络或超时 / -32602 参数缺失
    error_code: int | None = None


class ServiceHttpConfig:
    """单个服务的 HTTP 侧配置（由 registry 构造）。"""

    def __init__(self, base_url: str, auth: AuthConfig | None, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # trust_env=False：网关转发目标为内网 API，直连不走本机代理；
            # follow_redirects=False：不自动跟随目标 API 重定向（SSRF 防护，
            # 注册时已对 base_url 做 allowlist 校验，自动跟随可能被引向未校验主机）
            self._client = httpx.AsyncClient(
                timeout=self.timeout_s, trust_env=False, follow_redirects=False
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def _missing_env(names: list[str]) -> str | None:
    for name in names:
        if not os.environ.get(name):
            return name
    return None


def apply_auth(
    auth: AuthConfig | None, headers: dict[str, str], params: dict[str, Any]
) -> str | None:
    """把凭证注入 headers/params；返回错误信息（凭证环境变量缺失时）。"""
    if auth is None:
        return None
    if auth.type == "bearer":
        env = auth.token_env or ""
        token = os.environ.get(env) if env else None
        if not token:
            return f"网关未配置凭证环境变量 {env or '(token_env 未填写)'}"
        headers["Authorization"] = f"Bearer {token}"
    elif auth.type == "api_key":
        env = auth.value_env or ""
        value = os.environ.get(env) if env else None
        if not value:
            return f"网关未配置凭证环境变量 {env or '(value_env 未填写)'}"
        if auth.location == "query":
            params[auth.key_name] = value
        else:
            headers[auth.key_name] = value
    elif auth.type == "basic":
        user_env, pass_env = auth.user_env or "", auth.password_env or ""
        user = os.environ.get(user_env) if user_env else None
        password = os.environ.get(pass_env) if pass_env else None
        if not user or not password:
            missing = [n for n in (user_env, pass_env) if not os.environ.get(n)]
            return f"网关未配置凭证环境变量 {', '.join(missing) or '(user_env/password_env 未填写)'}"
        import base64

        cred = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    return None


def render_path(path_template: str, arguments: dict[str, Any], path_params: list[str]) -> str:
    """用 path 参数渲染 URL path 段（逐段 URL 编码）。"""
    rendered = path_template
    for name in path_params:
        value = arguments.get(name)
        if value is None:
            raise KeyError(name)
        rendered = rendered.replace("{" + name + "}", quote(str(value), safe=""))
    return rendered


def _split_form_fields(body: Any) -> tuple[dict[str, Any], list[tuple[str, tuple[Any, ...]]]]:
    """表单字段拆分：普通值 → data 字段；{filename, content[, encoding]} → 文件。"""
    data: dict[str, Any] = {}
    files: list[tuple[str, tuple[Any, ...]]] = []
    if not isinstance(body, dict):
        return {"value": body}, []
    for key, value in body.items():
        if isinstance(value, dict) and "filename" in value and "content" in value:
            content: Any = value["content"]
            if value.get("encoding") == "base64" and isinstance(content, str):
                content = base64.b64decode(content)
            files.append((key, (value["filename"], content, value.get("content_type"))))
        else:
            data[key] = value
    return data, files


def _truncate(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = text[: limit // 2]  # 粗略按字节折半截字符
    return cut + f"\n...(响应超过 {limit} 字节，已截断)"


class Invoker:
    def __init__(self, http_cfg: ServiceHttpConfig):
        self.cfg = http_cfg

    async def invoke(self, tool: ToolDef, arguments: dict[str, Any] | None) -> InvokeResult:
        ep = tool.endpoint
        arguments = arguments or {}

        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, Any] = {}
        auth_error = apply_auth(self.cfg.auth, headers, params)
        if auth_error:
            return InvokeResult(ok=False, error=auth_error, error_code=-32001)

        # 参数按位置分流
        path_params: list[str] = []
        for p in ep.params:
            if p.location == ParamLocation.path:
                # path 参数无条件参与渲染：缺失时 render_path 会显式报错
                path_params.append(p.name)
                continue
            value = arguments.get(p.name)
            if value is None:
                continue
            if p.location == ParamLocation.query:
                params[p.name] = value
            elif p.location == ParamLocation.header:
                headers[p.name] = str(value)
            elif p.location == ParamLocation.cookie:
                cookies = headers.get("Cookie")
                pair = f"{p.name}={value}"
                headers["Cookie"] = f"{cookies}; {pair}" if cookies else pair

        try:
            path = render_path(ep.path, arguments, path_params)
        except KeyError as exc:
            return InvokeResult(
                ok=False, error=f"缺少路径参数 {exc.args[0]}", error_code=-32602
            )

        body = arguments.get("body")
        url = f"{self.cfg.base_url}{path}"
        form_mt: str | None = None
        data: dict[str, Any] | None = None
        files: list[tuple[str, tuple[Any, ...]]] | None = None
        if body is not None:
            mt = (ep.body.media_type if ep.body else "") or "application/json"
            if mt == "application/x-www-form-urlencoded" or mt.startswith("multipart/"):
                # 表单提交：Content-Type 交给 httpx（multipart 需自动生成 boundary）
                form_mt = mt
                data, file_list = _split_form_fields(body)
                files = file_list or None
                if mt == "application/x-www-form-urlencoded":
                    files = None
            else:
                headers["Content-Type"] = mt

        try:
            resp = await self.cfg.client().request(
                ep.method.upper(),
                url,
                headers=headers,
                params=params or None,
                json=body if body is not None and form_mt is None else None,
                data=data,
                files=files,
            )
        except httpx.TimeoutException:
            return InvokeResult(
                ok=False,
                error=f"目标 API 超时（>{self.cfg.timeout_s}s）: {url}",
                error_code=-32002,
            )
        except httpx.HTTPError as exc:
            return InvokeResult(
                ok=False,
                error=f"网络错误: {exc.__class__.__name__}: {exc}",
                error_code=-32002,
            )

        if resp.is_success:
            try:
                payload: Any = resp.json()
                text = json.dumps(payload, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                payload = text = resp.text
            if len(text.encode("utf-8")) > SUCCESS_TRUNCATE_BYTES:
                return InvokeResult(
                    ok=True,
                    status_code=resp.status_code,
                    payload=_truncate(text, SUCCESS_TRUNCATE_BYTES),
                )
            return InvokeResult(ok=True, status_code=resp.status_code, payload=payload)

        excerpt = _truncate(resp.text or "", ERROR_BODY_EXCERPT_BYTES)
        return InvokeResult(
            ok=False,
            status_code=resp.status_code,
            error=f"目标 API 返回 HTTP {resp.status_code}" + (f": {excerpt}" if excerpt else ""),
            error_code=-32000,
        )
