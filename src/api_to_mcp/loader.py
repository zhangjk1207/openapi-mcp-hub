"""openapi 规范加载、校验与 $ref 解析。"""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import unquote

import httpx
import yaml

from .config import MAX_REF_DEPTH, MAX_SPEC_BYTES
from .errors import SpecError

SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete")


def parse_spec_text(text: str) -> dict[str, Any]:
    """JSON 优先，失败则按 YAML 解析。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"spec 既不是合法 JSON 也不是合法 YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"spec 顶层必须是对象，实际是 {type(data).__name__}")
    return data


def load_spec(
    source: dict[str, Any] | str, *, allowlist_check=None
) -> tuple[dict[str, Any], list[str]]:
    """加载 spec：dict 原样 / http(s) URL 拉取 / 本地文件路径。

    返回 (原始 spec, warnings)。不做 $ref 解析（那是 resolve_refs 的职责，
    持久化时保存原文，恢复时重新走流水线）。

    URL 拉取不自动跟随重定向：手动逐跳跟随（最多 5 跳），每跳过
    ``allowlist_check``（SSRF 防护：防 302 引向未校验主机）。
    """
    warnings: list[str] = []
    if isinstance(source, dict):
        return copy.deepcopy(source), warnings
    if not isinstance(source, str):
        raise SpecError(f"不支持的 spec 来源类型: {type(source).__name__}")

    if source.startswith(("http://", "https://")):
        url = source
        text: str | None = None
        for _hop in range(5):
            if allowlist_check is not None and not allowlist_check(url):
                raise SpecError(f"spec_url（或其重定向目标）不在 allowlist 内: {url}")
            try:
                resp = httpx.get(url, timeout=30.0, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise SpecError(f"拉取 spec_url 失败: {exc}") from exc
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if not location:
                    raise SpecError(f"spec_url 重定向缺少 Location 头: {url}")
                url = str(httpx.URL(url).join(location))
                if not url.startswith(("http://", "https://")):
                    raise SpecError(f"spec_url 重定向到不支持的协议: {url}")
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise SpecError(f"拉取 spec_url 失败: {exc}") from exc
            text = resp.text
            break
        if text is None:
            raise SpecError(f"spec_url 重定向次数超过上限（5）: {source}")
    else:
        try:
            text = open(source, "r", encoding="utf-8").read()
        except OSError as exc:
            raise SpecError(f"读取 spec 文件失败: {exc}") from exc

    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError(f"spec 体积超过上限 {MAX_SPEC_BYTES} 字节")
    return parse_spec_text(text), warnings


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """基础校验（在 normalize_spec 之后调用），返回 warnings；硬错误抛 SpecError。"""
    warnings: list[str] = []
    version = spec.get("openapi")
    if not version:
        if str(spec.get("swagger", "")).startswith("2."):
            raise SpecError("Swagger 2.0 spec 未经归一化（应先经 normalize_spec 转换）")
        raise SpecError("缺少 openapi/swagger 版本字段")
    if not str(version).startswith("3."):
        raise SpecError(f"不支持的 openapi 版本: {version}（仅支持 3.x）")
    if str(version).startswith("3.1"):
        warnings.append(
            "OpenAPI 3.1：外部 $ref 与 type 数组等特性仅部分支持（文档内 $ref 已支持）"
        )

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise SpecError("paths 必须是对象")
    if not paths:
        raise SpecError("paths 为空：没有可转换的接口")
    return warnings


# ---------------------------------------------------------------- Swagger 2.0 归一化

_REF_PREFIX_MAP = {
    "#/definitions/": "#/components/schemas/",
    "#/parameters/": "#/components/parameters/",
    "#/responses/": "#/components/responses/",
}

_INLINE_SCHEMA_KEYS = (
    "type", "format", "items", "enum", "default", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "pattern", "minLength", "maxLength",
    "uniqueItems", "multipleOf", "minItems", "maxItems",
)

_METHOD_KEYS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _rewrite_refs(node: Any) -> None:
    """递归重写 Swagger 2.0 的 $ref 前缀（#/definitions/ → #/components/schemas/ 等）。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            for old, new in _REF_PREFIX_MAP.items():
                if ref.startswith(old):
                    node["$ref"] = new + ref[len(old):]
                    break
        for value in node.values():
            _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item)


def _convert_param(p: dict[str, Any]) -> dict[str, Any] | None:
    """Swagger 2.0 非 body 参数 → OAS3：内联 type 包装成 schema，映射 collectionFormat。"""
    out = {k: v for k, v in p.items() if k not in ("collectionFormat", "type", "format",
                                                  "items", "enum", "default", "minimum",
                                                  "maximum", "pattern", "minLength",
                                                  "maxLength", "uniqueItems")}
    if "schema" not in out:
        schema = {k: p[k] for k in _INLINE_SCHEMA_KEYS if k in p}
        if schema:
            out["schema"] = schema
    if out.get("in") == "path":
        out["required"] = True
    if p.get("collectionFormat") == "multi":
        out["style"] = "form"
        out["explode"] = True
    return out


def _swagger2_to_oas3(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Swagger 2.0 → OpenAPI 3.0 结构归一（尽力转换，不追求全量语义等价）。

    转换内容：
    - host/basePath/schemes → servers[0]；
    - definitions/parameters/responses/securityDefinitions → components/*，$ref 前缀重写；
    - ``in: body`` 参数 → requestBody（application/json）；
    - ``in: formData`` 字段 → 合并为 application/json body 属性（并 warning）；
    - 非 body 参数内联 type → 包装为 schema，path 参数强制 required；
    - responses 的裸 schema → content.application/json.schema。
    """
    warnings: list[str] = []
    out = copy.deepcopy(doc)
    out.pop("swagger", None)
    out["openapi"] = "3.0.0"

    host = out.pop("host", None)
    base_path = out.pop("basePath", "") or ""
    schemes = out.pop("schemes", None) or ["https"]
    if host:
        out["servers"] = [{"url": f"{schemes[0]}://{host}{base_path}"}]

    components: dict[str, Any] = {}
    if "definitions" in out:
        components["schemas"] = out.pop("definitions")
    if "parameters" in out:
        components["parameters"] = out.pop("parameters")
    if "responses" in out:
        components["responses"] = out.pop("responses")
    if "securityDefinitions" in out:
        components["securitySchemes"] = out.pop("securityDefinitions")
    if components:
        out["components"] = components
    _rewrite_refs(out)

    for path, path_item in (out.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method not in _METHOD_KEYS or not isinstance(op, dict):
                continue
            new_params: list[dict[str, Any]] = []
            body_param: dict[str, Any] | None = None
            form_props: dict[str, Any] = {}
            form_required: list[str] = []
            for p in op.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                loc = p.get("in")
                if loc == "body":
                    body_param = {
                        "description": p.get("description", ""),
                        "required": bool(p.get("required", False)),
                        "content": {"application/json": {"schema": p.get("schema") or {}}},
                    }
                elif loc == "formData":
                    prop: dict[str, Any] = {
                        k: p[k] for k in ("type", "format", "enum", "default") if k in p
                    }
                    if prop.get("type") == "file":
                        prop = {"type": "string", "format": "binary"}
                    form_props[p.get("name", "")] = prop
                    if p.get("required"):
                        form_required.append(p.get("name", ""))
                else:
                    converted = _convert_param(p)
                    if converted:
                        new_params.append(converted)
            if form_props:
                warnings.append(
                    f"{method.upper()} {path}: Swagger 2.0 formData 参数已合并为 JSON body 属性（原为表单提交）"
                )
                if body_param is None:
                    body_param = {"content": {"application/json": {"schema": {}}}}
                schema = body_param["content"]["application/json"].setdefault("schema", {})
                if schema.get("type") == "object" or not schema:
                    schema.setdefault("type", "object")
                    props = schema.setdefault("properties", {})
                    props.update(form_props)
                    if form_required:
                        schema.setdefault("required", [])
                        for r in form_required:
                            if r not in schema["required"]:
                                schema["required"].append(r)
            if new_params or "parameters" in op:
                op["parameters"] = new_params
            if body_param is not None:
                op["requestBody"] = body_param
            for resp in (op.get("responses") or {}).values():
                if isinstance(resp, dict) and "schema" in resp and "content" not in resp:
                    resp["content"] = {"application/json": {"schema": resp.pop("schema")}}
    return out, warnings


def normalize_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """归一化：Swagger 2.0 → OpenAPI 3.0；OpenAPI 3.0/3.1 原样返回。

    返回 (归一后的 spec, warnings)；原文不做原地修改（持久化仍存原文）。
    """
    version = str(spec.get("swagger", ""))
    if version.startswith("2."):
        converted, warnings = _swagger2_to_oas3(spec)
        warnings.insert(0, f"已将 Swagger {version} spec 归一化为 OpenAPI 3.0 结构")
        return converted, warnings
    return spec, []


# ---------------------------------------------------------------- $ref 解析


def _pointer_get(doc: dict[str, Any], pointer: str) -> Any:
    """按 JSON pointer（#/a/b/0）取值。"""
    if not pointer.startswith("#/"):
        raise SpecError(f"不支持的外部 $ref（仅支持文档内 '#/' 开头）: {pointer}")
    node: Any = doc
    for raw in pointer[2:].split("/"):
        token = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                raise SpecError(f"$ref 指向不存在的位置: {pointer}")
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError) as exc:
                raise SpecError(f"$ref 指向不存在的位置: {pointer}") from exc
        else:
            raise SpecError(f"$ref 路径穿透了标量: {pointer}")
    return node


_CYCLE_PLACEHOLDER: dict[str, Any] = {
    "type": "object",
    "description": "(递归 $ref，已在解析时截断)",
}


def resolve_refs(spec: dict[str, Any]) -> dict[str, Any]:
    """解析文档内全部 $ref，返回深拷贝后的新文档。

    - 循环引用以占位 schema 截断；
    - 嵌套深度超过 MAX_REF_DEPTH 抛 SpecError（防解析炸弹）。
    """

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in stack:
                    return copy.deepcopy(_CYCLE_PLACEHOLDER)
                if len(stack) >= MAX_REF_DEPTH:
                    raise SpecError(f"$ref 嵌套深度超过 {MAX_REF_DEPTH}")
                target = _pointer_get(spec, ref)
                # 目标节点本身可能继续含 $ref，递归解析
                return walk(target, stack + (ref,))
            return {k: walk(v, stack) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(item, stack) for item in node]
        return node

    return walk(spec, ())


def resolve_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """校验 + $ref 解析一步到位。"""
    validate_spec(spec)
    return resolve_refs(spec)
