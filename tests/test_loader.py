"""loader：解析 / 校验 / $ref。"""

import json

import pytest

from api_to_mcp.config import MAX_REF_DEPTH
from api_to_mcp.errors import SpecError
from api_to_mcp.loader import parse_spec_text, resolve_refs, validate_spec


class TestParse:
    def test_json(self):
        data = parse_spec_text('{"openapi": "3.0.0", "paths": {}}')
        assert data["openapi"] == "3.0.0"

    def test_yaml(self):
        data = parse_spec_text("openapi: 3.0.0\npaths: {}")
        assert data["openapi"] == "3.0.0"

    def test_invalid(self):
        with pytest.raises(SpecError):
            parse_spec_text(":::: not json nor yaml ::::\t{")

    def test_non_object_top(self):
        with pytest.raises(SpecError):
            parse_spec_text("[1, 2, 3]")


class TestValidate:
    def test_ok(self, petstore_spec):
        assert validate_spec(petstore_spec) == []

    def test_missing_openapi(self):
        with pytest.raises(SpecError, match="openapi"):
            validate_spec({"paths": {"/x": {"get": {}}}})

    def test_swagger_2_rejected(self):
        with pytest.raises(SpecError, match="Swagger 2.0"):
            validate_spec({"swagger": "2.0", "paths": {"/x": {"get": {}}}})

    def test_empty_paths(self):
        with pytest.raises(SpecError, match="paths 为空"):
            validate_spec({"openapi": "3.0.0", "paths": {}})

    def test_unsupported_version(self):
        with pytest.raises(SpecError, match="版本"):
            validate_spec({"openapi": "4.0.0", "paths": {"/x": {"get": {}}}})


class TestResolveRefs:
    def test_nested_ref_resolved(self, petstore_spec):
        resolved = resolve_refs(petstore_spec)
        body_schema = resolved["paths"]["/pets"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        # NewPet 被内联，不再有 $ref，字段保留
        assert "$ref" not in json.dumps(body_schema)
        assert body_schema["properties"]["name"]["type"] == "string"
        assert body_schema["required"] == ["name"]
        # items 里的 $ref（Pet）同样内联
        list_resp = resolved["paths"]["/pets"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert list_resp["items"]["required"] == ["id", "name"]

    def test_circular_ref_truncated(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/A"}
                                    }
                                },
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "A": {
                        "type": "object",
                        "properties": {"b": {"$ref": "#/components/schemas/B"}},
                    },
                    "B": {
                        "type": "object",
                        "properties": {"a": {"$ref": "#/components/schemas/A"}},
                    },
                }
            },
        }
        resolved = resolve_refs(spec)
        assert "递归 $ref" in json.dumps(resolved, ensure_ascii=False)

    def test_external_ref_rejected(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "https://evil.example/x.json#/A"}
                                    }
                                },
                            }
                        }
                    }
                }
            },
        }
        with pytest.raises(SpecError, match="外部"):
            resolve_refs(spec)

    def test_broken_pointer_rejected(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/no/such/thing"}
                                    }
                                },
                            }
                        }
                    }
                }
            },
        }
        with pytest.raises(SpecError, match="不存在"):
            resolve_refs(spec)

    def test_depth_bomb_rejected(self):
        # 构造超深引用链（防解析炸弹）
        schemas: dict = {"s0": {"type": "object", "properties": {"v": {"type": "string"}}}}
        for i in range(1, MAX_REF_DEPTH + 5):
            schemas[f"s{i}"] = {
                "type": "object",
                "properties": {"next": {"$ref": f"#/components/schemas/s{i - 1}"}},
            }
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": f"#/components/schemas/s{MAX_REF_DEPTH + 4}"
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            },
            "components": {"schemas": schemas},
        }
        with pytest.raises(SpecError, match="深度"):
            resolve_refs(spec)


# ---------------------------------------------------------------- Swagger 2.0 / 3.1 / 重定向抓取

SWAGGER2_SPEC = {
    "swagger": "2.0",
    "info": {"title": "Legacy API", "version": "1.0"},
    "host": "legacy.corp",
    "basePath": "/api",
    "schemes": ["https"],
    "paths": {
        "/users/{userId}": {
            "get": {
                "operationId": "getUser",
                "summary": "查用户",
                "parameters": [
                    {"name": "userId", "in": "path", "type": "integer", "required": True},
                    {"name": "verbose", "in": "query", "type": "boolean"},
                    {"name": "X-Trace", "in": "header", "type": "string"},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {"$ref": "#/definitions/User"},
                    }
                },
            }
        },
        "/users": {
            "post": {
                "operationId": "createUser",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {"$ref": "#/definitions/User"},
                    }
                ],
                "responses": {"201": {"description": "Created"}},
            },
            "put": {
                "operationId": "updateUser",
                "parameters": [
                    {"name": "uid", "in": "formData", "type": "string", "required": True},
                    {"name": "nick", "in": "formData", "type": "string"},
                ],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
    "definitions": {
        "User": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
            },
            "required": ["id", "name"],
        }
    },
}


class TestSwagger2Normalize:
    def test_normalize_structure(self):
        from api_to_mcp.loader import normalize_spec

        spec, warnings = normalize_spec(SWAGGER2_SPEC)
        assert spec["openapi"] == "3.0.0"
        assert "swagger" not in spec and "host" not in spec
        assert spec["servers"] == [{"url": "https://legacy.corp/api"}]
        assert "components" in spec and "schemas" in spec["components"]
        # definitions → components.schemas
        assert "User" in spec["components"]["schemas"]
        assert "definitions" not in spec
        assert any("Swagger 2.0" in w for w in warnings)
        # formData 警告
        assert any("formData" in w for w in warnings)

    def test_refs_rewritten(self):
        from api_to_mcp.loader import normalize_spec

        spec, _ = normalize_spec(SWAGGER2_SPEC)
        op = spec["paths"]["/users/{userId}"]["get"]
        assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/User"
        }
        body = spec["paths"]["/users"]["post"]["requestBody"]
        assert body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/User"}
        assert body["required"] is True

    def test_inline_params_get_schema(self):
        from api_to_mcp.loader import normalize_spec

        spec, _ = normalize_spec(SWAGGER2_SPEC)
        params = spec["paths"]["/users/{userId}"]["get"]["parameters"]
        by_name = {p["name"]: p for p in params}
        # path 参数：内联 type 包装成 schema + 强制 required
        assert by_name["userId"]["schema"] == {"type": "integer"}
        assert by_name["userId"]["required"] is True
        assert by_name["verbose"]["schema"] == {"type": "boolean"}
        assert "collectionFormat" not in str(params)

    def test_formdata_merged_into_body(self):
        from api_to_mcp.loader import normalize_spec

        spec, _ = normalize_spec(SWAGGER2_SPEC)
        schema = spec["paths"]["/users"]["put"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"uid", "nick"}
        assert schema["required"] == ["uid"]

    def test_swagger_full_pipeline_to_tools(self):
        """Swagger 2.0 全流水线：normalize → resolve → extract → generate。"""
        from api_to_mcp.extractor import extract_endpoints
        from api_to_mcp.generator import generate_tools
        from api_to_mcp.loader import normalize_spec, resolve_refs, validate_spec
        from api_to_mcp.models import Filters

        spec, _ = normalize_spec(SWAGGER2_SPEC)
        validate_spec(spec)
        resolved = resolve_refs(spec)
        endpoints, _ = extract_endpoints(resolved, Filters())
        tools, _ = generate_tools(endpoints, max_tools=100)
        by_name = {t.name: t for t in tools}
        assert set(by_name) == {"get_user", "create_user", "update_user"}
        # path 参数必填 + integer 类型透传（参数名保留原文，invoker 靠它渲染路径）
        assert by_name["get_user"].input_schema["required"] == ["userId"]
        assert by_name["get_user"].input_schema["properties"]["userId"]["type"] == "integer"
        # body 参数：$ref 已解析展开
        assert "properties" in by_name["create_user"].input_schema["properties"]["body"]
        # formData → body 属性
        assert set(by_name["update_user"].input_schema["properties"]["body"]["properties"]) == {
            "uid", "nick"
        }
        assert by_name["update_user"].input_schema["properties"]["body"]["required"] == ["uid"]

    def test_oas3_passthrough(self):
        from api_to_mcp.loader import normalize_spec

        oas3 = {"openapi": "3.0.0", "paths": {"/x": {"get": {}}}}
        spec, warnings = normalize_spec(oas3)
        assert spec is oas3 and warnings == []


class TestOpenApi31:
    def test_31_accepted_with_warning(self):
        spec31 = {
            "openapi": "3.1.0",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "getX",
                        "parameters": [
                            # 3.1 允许 type 数组与布尔 schema
                            {"name": "flag", "in": "query", "schema": True},
                            {"name": "kind", "in": "query", "schema": {"type": ["string", "null"]}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        warnings = validate_spec(spec31)
        assert any("3.1" in w for w in warnings)

        from api_to_mcp.extractor import extract_endpoints
        from api_to_mcp.loader import resolve_refs
        from api_to_mcp.models import Filters

        endpoints, _ = extract_endpoints(resolve_refs(spec31), Filters())
        params = {p.name: p for p in endpoints[0].params}
        # 布尔 schema 归一化为空 dict（不混进 input_schema）
        assert params["flag"].schema == {}
        assert params["kind"].schema == {"type": ["string", "null"]}


class TestRedirectSafeFetch:
    def test_redirect_each_hop_checked(self, respx_mock):
        import httpx

        from api_to_mcp.loader import load_spec

        spec_body = json.dumps({"openapi": "3.0.0", "paths": {"/x": {"get": {}}}})

        def allow_good(url: str) -> bool:
            return "good" in url

        respx_mock.get("https://good.example/spec").mock(
            return_value=httpx.Response(302, headers={"Location": "https://good.example/real-spec"})
        )
        respx_mock.get("https://good.example/real-spec").mock(
            return_value=httpx.Response(200, text=spec_body)
        )
        spec, _ = load_spec("https://good.example/spec", allowlist_check=allow_good)
        assert spec["openapi"] == "3.0.0"

        # 重定向到未授权主机 => 拒绝
        respx_mock.get("https://good.example/spec2").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        )
        with pytest.raises(SpecError, match="allowlist"):
            load_spec("https://good.example/spec2", allowlist_check=allow_good)

    def test_redirect_loop_bounded(self, respx_mock):
        import httpx

        from api_to_mcp.loader import load_spec

        respx_mock.get("https://good.example/loop").mock(
            return_value=httpx.Response(302, headers={"Location": "https://good.example/loop"})
        )
        with pytest.raises(SpecError, match="重定向次数"):
            load_spec("https://good.example/loop", allowlist_check=lambda url: True)

    def test_relative_location_joined(self, respx_mock):
        import httpx

        from api_to_mcp.loader import load_spec

        respx_mock.get("https://good.example/a/spec").mock(
            return_value=httpx.Response(301, headers={"Location": "/b/spec"})
        )
        respx_mock.get("https://good.example/b/spec").mock(
            return_value=httpx.Response(200, text='{"openapi": "3.0.0", "paths": {}}')
        )
        spec, _ = load_spec("https://good.example/a/spec", allowlist_check=lambda url: True)
        assert spec["openapi"] == "3.0.0"
