"""ApiEndpoint -> MCP ToolDef：命名规则与输入 schema 生成。"""

from __future__ import annotations

import re
from typing import Any

from .annotations import infer_annotations
from .compact import FIELD_DESCRIPTION_LIMIT, compact_description, compact_schema, compact_text
from .config import DEFAULT_MAX_TOOLS
from .errors import RegistrationError
from .models import ApiEndpoint, ParamLocation, ToolDef

_MAX_NAME_LEN = 64
# MCP 工具名建议字符集
_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")

# 透传给 LLM 的 JSON Schema 中需要剔除的 OpenAPI 噪音键
_SCHEMA_NOISE_KEYS = ("example", "examples", "xml", "externalDocs", "discriminator", "readOnly")


def _clean_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """递归剔除噪音键，产出干净的 JSON Schema（$ref 已在 loader 解析）。"""
    if not isinstance(schema, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _SCHEMA_NOISE_KEYS:
            continue
        if isinstance(v, dict):
            out[k] = _clean_schema(v)
        elif isinstance(v, list):
            out[k] = [_clean_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def snake_case(text: str) -> str:
    s = _CAMEL_1.sub(r"\1_\2", text)
    s = _CAMEL_2.sub(r"\1_\2", s)
    return s


def sanitize_tool_name(text: str) -> str:
    name = snake_case(text)
    name = _INVALID_CHARS.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_").lower()
    return name[:_MAX_NAME_LEN].rstrip("_-") or "tool"


def path_slug(path: str) -> str:
    """'/users/{userId}' -> 'users_userid'。"""
    slug = re.sub(r"[{}]", "", path)
    return sanitize_tool_name(slug) or "root"


def fallback_tool_name(endpoint: ApiEndpoint) -> str:
    return f"{endpoint.method}_{path_slug(endpoint.path)}"


def build_description(endpoint: ApiEndpoint) -> str:
    """拼装工具描述，并做无损压缩（详见 compact.py）。

    顺序有意如此：先用原文拼出完整信息，再整体压缩。逐段压缩会让摘要和
    HTTP 行使不上压缩阈值，反而更容易超限。
    """
    parts: list[str] = []
    if endpoint.summary:
        parts.append(endpoint.summary.strip())
    if endpoint.description and endpoint.description.strip() != (endpoint.summary or "").strip():
        parts.append(endpoint.description.strip())
    parts.append(f"HTTP {endpoint.method.upper()} {endpoint.path}")
    if endpoint.deprecated:
        parts.append("⚠️ 该接口在 openapi 中已标记 deprecated")
    return compact_description("\n\n".join(p for p in parts if p))


def build_input_schema(endpoint: ApiEndpoint) -> dict[str, Any]:
    """path/query/header 参数 -> 顶层属性；requestBody -> 整体挂 body 属性。"""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in endpoint.params:
        prop = _clean_schema(param.schema) or {"type": "string"}
        if not isinstance(prop, dict):
            prop = {"type": "string"}
        desc = compact_text(
            param.description or prop.get("description") or f"{param.location.value} 参数 {param.name}",
            FIELD_DESCRIPTION_LIMIT,
        )
        prop = {**prop, "description": desc}
        # path 参数在 openapi 语义里恒为必填
        if param.location == ParamLocation.path:
            prop.pop("default", None)
        properties[param.name] = prop
        if param.required or param.location == ParamLocation.path:
            if param.name not in required:
                required.append(param.name)

    if endpoint.body is not None:
        body_schema = compact_schema(_clean_schema(endpoint.body.schema))
        desc = compact_text(
            endpoint.body.description or f"请求体（{endpoint.body.media_type}）",
            FIELD_DESCRIPTION_LIMIT,
        )
        properties["body"] = {**body_schema, "description": desc} if body_schema else {
            "type": "object",
            "description": desc,
        }
        if endpoint.body.required and "body" not in required:
            required.append("body")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        # 提示 LLM：未列出的属性不要编造
        "additionalProperties": False,
    }


def generate_tools(
    endpoints: list[ApiEndpoint], max_tools: int | None = None
) -> tuple[list[ToolDef], list[str]]:
    """生成 ToolDef 列表；名称冲突自动加序号；超限抛 RegistrationError。"""
    limit = max_tools or DEFAULT_MAX_TOOLS
    if len(endpoints) > limit:
        raise RegistrationError(
            f"接口数 {len(endpoints)} 超过工具数上限 {limit}；"
            "请用 filters.include 收窄，或按 tag 拆分为多个服务注册",
        )

    warnings: list[str] = []
    tools: list[ToolDef] = []
    used: dict[str, int] = {}

    for ep in endpoints:
        if ep.operation_id:
            base = sanitize_tool_name(str(ep.operation_id))
            if not ep.operation_id.isidentifier():
                pass  # 清洗属常规操作，不告警
        else:
            base = sanitize_tool_name(fallback_tool_name(ep))
            warnings.append(f"{ep.method.upper()} {ep.path} 缺少 operationId，自动命名 {base}")

        name = base
        if name in used:
            used[base] += 1
            suffix = f"_{used[base]}"
            name = f"{base[: _MAX_NAME_LEN - len(suffix)]}{suffix}"
            warnings.append(f"工具名冲突：{base} -> {name}")
        else:
            used[base] = 1

        tools.append(
            ToolDef(
                name=name,
                description=build_description(ep),
                input_schema=build_input_schema(ep),
                endpoint=ep,
                # 目标域固定为注册时的 base_url => 封闭世界
                annotations=infer_annotations(
                    ep.method, name=name, path=ep.path, closed_world=True
                ),
            )
        )
    return tools, warnings
