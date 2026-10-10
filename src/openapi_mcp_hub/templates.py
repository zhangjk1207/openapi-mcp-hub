"""响应模板：把目标 API 的原始响应裁剪成 LLM 友好的文本（借鉴 Unla responseBody）。

语法（刻意保持极简）：

- ``{{response}}``             整个响应体（dict/list 自动序列化为紧凑 JSON）
- ``{{response.user.name}}``   点路径安全取值（safeGet 语义：缺字段/越界 → 回退值，绝不抛错）
- ``{{response.items.0.id}}``  列表按下标访问
- ``{{response.user.name || 未登记}}``  ``||`` 后为默认值：取值为空/缺失时使用（safeGetOr 语义）
- ``{{status}}``               HTTP 状态码
- ``{{tool}}``                 工具名
- ``{{ok}}``                   恒为 "true"（模板仅在调用成功时渲染）
- ``{{}}`` 之外的内容原样保留；默认值内不能出现 ``{{``/``}}``

示例（注册请求体）::

    "response_template": "注册成功 {{response.name}}（id={{response.id}}）"

渲染失败时调用方回退为 passthrough，绝不影响调用结果本身。
"""

from __future__ import annotations

import json
import re
from typing import Any

_PLACEHOLDER = re.compile(
    r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)(?:\s*\|\|\s*([^{}]*?))?\s*\}\}"
)
_ALLOWED_ROOTS = ("response", "status", "tool", "ok")


class TemplateError(ValueError):
    """模板语法不合法（注册时校验抛出，由 registry 转 422）。"""


def validate_template(template: str) -> None:
    """校验模板：括号配平 + 占位符根变量合法。非法抛 TemplateError。"""
    if not isinstance(template, str) or not template.strip():
        raise TemplateError("模板不能为空")
    if template.count("{{") != template.count("}}"):
        raise TemplateError("模板 {{ 与 }} 括号不配平")
    for m in _PLACEHOLDER.finditer(template):
        root = m.group(1).split(".", 1)[0]
        if root not in _ALLOWED_ROOTS:
            raise TemplateError(
                f"不支持的模板变量 {m.group(1)!r}（可用根变量: {'/'.join(_ALLOWED_ROOTS)}）"
            )
    # 括号数量配平但仍存在无法解析的 {{...}}（如 {{foo-bar}}、{{ response }} 内含非法字符）
    leftover = _PLACEHOLDER.sub("", template)
    if "{{" in leftover or "}}" in leftover:
        raise TemplateError("模板存在无法解析的占位符（仅支持 {{变量.路径}} 形式）")


class _Missing:
    """safeGet 哨兵：路径中断时返回，最终渲染为空串。"""


def _safe_step(value: Any, seg: str) -> Any:
    """单段取值：dict 按 key，list 按整数下标；类型不符/越界/缺失 → _Missing。"""
    if isinstance(value, dict):
        return value[seg] if seg in value else _Missing
    if isinstance(value, list):
        try:
            return value[int(seg)]
        except (ValueError, IndexError):
            return _Missing
    return _Missing


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_response(template: str, *, response: Any, status: int, tool: str) -> str:
    """渲染模板。任何占位符取值失败都回退默认值/空串，本函数自身不抛错。"""

    def eval_expr(expr: str) -> str:
        root, *path = expr.split(".")
        if root == "response":
            value: Any = response
            for seg in path:
                value = _safe_step(value, seg)
                if value is _Missing:
                    return ""
            return _to_text(value)
        if root == "status":
            return str(status)
        if root == "tool":
            return tool
        if root == "ok":
            return "true"
        return ""  # validate_template 已挡住，保险起见

    def repl(m: re.Match[str]) -> str:
        value = eval_expr(m.group(1))
        if value:
            return value
        fallback = (m.group(2) or "").strip()
        return fallback

    return _PLACEHOLDER.sub(repl, template)
