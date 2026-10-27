"""Invoker：把 MCP 工具调用还原为真实 HTTP 请求。

设计要点（与 README v2 对齐）：
- per-service 独立 httpx client（独立超时/连接池）；
- 认证凭证只从环境变量读取，绝不落盘；
- 非 2xx / 网络异常 => isError 结构化结果；
- 成功响应超限截断、错误响应体摘要 4KB。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .models import AuthConfig, ParamLocation, ToolDef

SUCCESS_TRUNCATE_BYTES = 64 * 1024
ERROR_BODY_EXCERPT_BYTES = 4 * 1024

# 凭证类错误（-32001）统一提示：属于服务端配置问题，agent 重试无意义。
_CRED_HINT = (
    "凭证由服务管理员通过环境变量配置，属于服务端问题："
    "重试不会成功，请改用其它工具或向管理员反馈。"
)

# 按上游状态码给出的可自纠正提示。原则：明确告诉 agent「改参数」还是
# 「等一等再试」还是「重试无效，去找人」。含糊的“调用失败”会让 agent 反复盲试。
_STATUS_HINTS: dict[int, str] = {
    400: "请求体或参数不合法。按 inputSchema 逐字段核对类型与必填项后重试，不要原样重发。",
    401: "目标 API 拒绝了凭证。这是服务端配置问题（环境变量未配或已失效），重试不会成功。",
    403: "凭证有效但无权限访问该资源。换个允许的资源，或联系服务管理员，重试无效。",
    404: "路径或资源不存在。检查 id 类参数取值是否正确、资源是否已被删除；重试无效。",
    405: "该接口不允许此 HTTP 方法。确认选错了工具（可能是相邻的另一个工具）。",
    409: "资源当前状态与请求冲突。先用只读工具查询当前状态，再决定是否重试。",
    413: "请求体过大。减小 body 体积或改用分页/分批提交。",
    415: "请求体媒体类型不受支持。检查 body 的结构与 Content-Type。",
    422: "字段语义校验失败（类型对但值不合法，如枚举/格式/范围）。按错误信息里的字段名修正。",
    429: "上游限流。等待后重试，并降低调用频率。",
    500: "上游服务端错误。可稍后重试一次；连续失败说明上游自身异常，不要继续重试。",
    502: "上游网关错误。可稍后重试一次。",
    503: "上游不可用（可能正在重启）。等待后重试。",
    504: "上游超时。减小 limit/分页大小或稍后重试。",
}


def status_hint(status_code: int) -> str:
    """根据 HTTP 状态码给出建议；未列出的大类给保守结论。"""
    if status_code in _STATUS_HINTS:
        return _STATUS_HINTS[status_code]
    if 400 <= status_code < 500:
        return "目标 API 拒绝了本次请求。按错误摘要修正参数后重试，不要原样重发。"
    if status_code >= 500:
        return "上游服务端错误。可稍后重试一次；连续失败请检查上游健康。"
    return "目标 API 返回了非预期的状态码，详见错误摘要。"


@dataclass
class InvokeResult:
    ok: bool
    status_code: int | None = None
    payload: Any = None  # ok=True 时：解析后的 JSON 或文本
    error: str | None = None  # ok=False 时：人可读错误说明
    # 错误分类码（JSON-RPC 服务器自定义区间）：
    # -32000 目标 API 非 2xx / -32001 凭证缺失 / -32002 网络或超时 / -32602 参数缺失
    error_code: int | None = None
    # 给 LLM 的下一步建议（可自纠正）：写明「改什么」或「重试是否有用」
    hint: str | None = None


class ServiceHttpConfig:
    """单个服务的 HTTP 侧配置（由 registry 构造）。"""

    def __init__(self, base_url: str, auth: AuthConfig | None, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None
        # oauth2 client_credentials token 缓存（提前 60s 失效；锁住并发下的取 token 竞争）
        self._oauth2_token: str | None = None
        self._oauth2_expires_at: float = 0.0
        self._oauth2_lock = asyncio.Lock()

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
    elif auth.type == "oauth2":
        # token 获取/缓存/刷新在 Invoker._oauth2_token（异步）；此处只做环境存在性自检
        missing = [
            n
            for n in (auth.client_id_env, auth.client_secret_env)
            if not n or not os.environ.get(n)
        ]
        if missing:
            return f"网关未配置凭证环境变量 {', '.join(missing)}"
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

    async def _oauth2_token(self, *, force: bool = False) -> tuple[str | None, str | None]:
        """获取（并缓存）client_credentials token；返回 (token, 错误信息)。

        - 缓存在 ServiceHttpConfig 上，进程内复用；提前 60s 失效防边界拿着将过期 token 出门；
        - 双检锁：并发调用只触发一次 token 请求；
        - force=True（401 重试路径）无条件刷新；
        - token 请求走该服务同一 client（trust_env=False、不跟随重定向），
          token_url 已在注册期过 allowlist。
        """
        auth = self.cfg.auth
        assert auth is not None and auth.type == "oauth2"
        if not force and self.cfg._oauth2_token and time.monotonic() < self.cfg._oauth2_expires_at:
            return self.cfg._oauth2_token, None
        async with self.cfg._oauth2_lock:
            if not force and self.cfg._oauth2_token and time.monotonic() < self.cfg._oauth2_expires_at:
                return self.cfg._oauth2_token, None
            form = {
                "grant_type": "client_credentials",
                "client_id": os.environ.get(auth.client_id_env or ""),
                "client_secret": os.environ.get(auth.client_secret_env or ""),
            }
            scope = os.environ.get(auth.scope_env) if auth.scope_env else auth.scope
            if scope:
                form["scope"] = scope
            try:
                resp = await self.cfg.client().post(
                    auth.token_url, data=form, headers={"Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                return None, f"获取 oauth2 token 失败: {exc.__class__.__name__}: {exc}"
            if not resp.is_success:
                excerpt = _truncate(resp.text or "", ERROR_BODY_EXCERPT_BYTES)
                return None, (
                    f"token 端点返回 HTTP {resp.status_code}" + (f": {excerpt}" if excerpt else "")
                )
            try:
                payload = resp.json()
            except ValueError:
                return None, "token 端点响应不是合法 JSON"
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                return None, "token 端点响应缺少 access_token"
            try:
                ttl = int(payload.get("expires_in", 3600))
            except (TypeError, ValueError):
                ttl = 3600
            self.cfg._oauth2_token = token
            self.cfg._oauth2_expires_at = time.monotonic() + max(ttl - 60, 30)
            return token, None

    async def invoke(self, tool: ToolDef, arguments: dict[str, Any] | None) -> InvokeResult:
        ep = tool.endpoint
        arguments = arguments or {}

        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, Any] = {}
        auth_error = apply_auth(self.cfg.auth, headers, params)
        if auth_error:
            return InvokeResult(
                ok=False, error=auth_error, error_code=-32001,
                hint=_CRED_HINT,
            )

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
                ok=False,
                error=f"缺少路径参数 {exc.args[0]}",
                error_code=-32602,
                hint="path 参数是 URL 的一部分，必须提供具体值，不能留空或用占位符。",
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

        # oauth2：请求前确保 token（缓存命中则零网络开销）
        oauth2 = self.cfg.auth is not None and self.cfg.auth.type == "oauth2"
        if oauth2:
            token, token_err = await self._oauth2_token()
            if token_err:
                return InvokeResult(
                    ok=False, error=token_err, error_code=-32001, hint=_CRED_HINT
                )
            headers["Authorization"] = f"Bearer {token}"

        # oauth2 额外给一次 401 重试：token 在边界过期/被吊销时强制刷新后重发；
        # 静态凭证类型重试无意义（无旋转机制），不重试
        resp: httpx.Response | None = None
        for attempt in range(2 if oauth2 else 1):
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
                    hint=(
                        "超时。减小分页/limit 参数、缩小时间范围或简化查询后重试；"
                        "若反复超时请联系服务管理员检查上游响应时间。"
                    ),
                )
            except httpx.HTTPError as exc:
                return InvokeResult(
                    ok=False,
                    error=f"网络错误: {exc.__class__.__name__}: {exc}",
                    error_code=-32002,
                    hint="网关到目标 API 网络不通。检查 base_url 与网络策略；重试通常无效。",
                )
            if resp.status_code == 401 and oauth2 and attempt == 0:
                token, token_err = await self._oauth2_token(force=True)
                if token_err:
                    return InvokeResult(
                        ok=False, error=token_err, error_code=-32001,
                        hint=_CRED_HINT,
                    )
                headers["Authorization"] = f"Bearer {token}"
                continue
            break
        assert resp is not None

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
            hint=status_hint(resp.status_code),
        )
