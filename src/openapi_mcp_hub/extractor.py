"""从（已解析 $ref 的）openapi spec 中提取接口信息。"""

from __future__ import annotations

import fnmatch
from typing import Any

from .errors import SpecError
from .loader import SUPPORTED_METHODS
from .models import ApiBody, ApiEndpoint, ApiParam, Filters, ParamLocation

_SKIP_METHODS = ("head", "options", "trace", "parameters", "summary", "description", "servers", "$ref")
_PARAM_LOCATIONS = {loc.value: loc for loc in ParamLocation}


def _merge_params(path_item: dict[str, Any], operation: dict[str, Any]) -> list[ApiParam]:
    """合并 path 级与 operation 级 parameters（operation 级优先）。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in (path_item.get("parameters") or []) + (operation.get("parameters") or []):
        name = raw.get("name")
        location = raw.get("in")
        if not name or location not in _PARAM_LOCATIONS:
            continue
        merged[(name, location)] = raw  # 后写覆盖 => operation 级优先

    params: list[ApiParam] = []
    for (name, location), raw in merged.items():
        # OpenAPI 3.1 允许布尔 schema（true=任意/false=永不）——归一化为空 dict，
        # 避免 True/False 混进 input_schema 破坏下游（LLM 客户端普遍不支持布尔 schema）
        raw_schema = raw.get("schema")
        if raw_schema is True or raw_schema is False:
            raw_schema = {}
        params.append(
            ApiParam(
                name=name,
                location=_PARAM_LOCATIONS[location],
                required=bool(raw.get("required", False)),
                schema=raw_schema or {},
                description=raw.get("description") or "",
            )
        )
    return params


def _extract_body(operation: dict[str, Any], warnings: list[str]) -> ApiBody | None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content") or {}
    media_type = next((mt for mt in content if mt == "application/json" or mt.endswith("+json")), None)
    if media_type is None:
        # 表单类 body：字段同样提为 body 参数，invoker 按表单方式提交（Unla form-data 语义）
        form = next(
            (
                mt
                for mt in content
                if mt == "application/x-www-form-urlencoded"
                or mt.startswith("multipart/")
            ),
            None,
        )
        if form is not None:
            media_type = form
            warnings.append(
                f"requestBody 为 {form}：按表单字段提交；字段值传 {{filename, content}} 即作为文件上传"
            )
    if media_type is None:
        warnings.append(
            f"requestBody 无 JSON content-type（{sorted(content)}），已忽略 body，仅保留参数"
        )
        return None
    schema = (content[media_type] or {}).get("schema")
    if schema is True or schema is False:  # OpenAPI 3.1 布尔 schema → 空 dict
        schema = {}
    return ApiBody(
        schema=schema,
        required=bool(request_body.get("required", False)),
        description=request_body.get("description") or "",
        media_type=media_type,
    )


def extract_endpoints(spec: dict[str, Any], filters: Filters) -> tuple[list[ApiEndpoint], list[str]]:
    """遍历 paths × method，产出 ApiEndpoint 列表；返回 (endpoints, warnings)。"""
    warnings: list[str] = []
    endpoints: list[ApiEndpoint] = []

    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            warnings.append(f"paths['{path}'] 不是对象，已跳过")
            continue
        if not isinstance(path, str) or not path.startswith("/"):
            warnings.append(f"非法路径 '{path}'，已跳过")
            continue

        # 路径级过滤
        if filters.include and not any(fnmatch.fnmatch(path, p) for p in filters.include):
            continue
        if filters.exclude and any(fnmatch.fnmatch(path, p) for p in filters.exclude):
            continue

        for method, operation in path_item.items():
            if method in _SKIP_METHODS or not isinstance(operation, dict):
                if method in ("head", "options", "trace"):
                    warnings.append(f"{method.upper()} {path} 已跳过（M1 仅支持 {', '.join(m.upper() for m in SUPPORTED_METHODS)}）")
                continue
            if method not in SUPPORTED_METHODS:
                continue
            if not isinstance(operation, dict):
                continue

            tags = [t for t in (operation.get("tags") or []) if isinstance(t, str)]
            if filters.exclude_tags and set(tags) & set(filters.exclude_tags):
                continue
            deprecated = bool(operation.get("deprecated", False))
            if deprecated and not filters.include_deprecated:
                warnings.append(f"{method.upper()} {path} 已标记 deprecated，默认跳过（include_deprecated 可保留）")
                continue

            endpoints.append(
                ApiEndpoint(
                    method=method,
                    path=path,
                    operation_id=operation.get("operationId"),
                    summary=operation.get("summary") or "",
                    description=operation.get("description") or "",
                    deprecated=deprecated,
                    tags=tags,
                    params=_merge_params(path_item, operation),
                    body=_extract_body(operation, warnings),
                )
            )

    if not endpoints:
        raise SpecError("过滤后没有任何可转换的接口（检查 include/exclude/exclude_tags/deprecated 配置）")
    return endpoints, warnings
