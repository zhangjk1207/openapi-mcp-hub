"""generator：命名规则 / schema 合并 / 上限。"""

import pytest

from api_to_mcp.errors import RegistrationError
from api_to_mcp.generator import (
    build_input_schema,
    generate_tools,
    sanitize_tool_name,
)
from api_to_mcp.models import ApiBody, ApiEndpoint, ApiParam, ParamLocation


def _ep(**kwargs) -> ApiEndpoint:
    defaults = dict(method="get", path="/pets/{petId}", operation_id=None, summary="", description="")
    defaults.update(kwargs)
    return ApiEndpoint(**defaults)


class TestNaming:
    def test_camel_case(self):
        assert sanitize_tool_name("getPetById") == "get_pet_by_id"

    def test_illegal_chars(self):
        # 非 ASCII（中文等）会被清洗掉，仅保留合法字符
        assert sanitize_tool_name("list 宠物(详情)!") == "list"

    def test_pure_non_ascii_falls_back(self):
        assert sanitize_tool_name("查询宠物") == "tool"

    def test_max_length(self):
        assert len(sanitize_tool_name("a" * 100)) == 64

    def test_empty_falls_back(self):
        assert sanitize_tool_name("---") == "tool"

    def test_fallback_from_path(self):
        tools, warnings = generate_tools([_ep(operation_id=None)])
        # /pets/{petId} -> pets_petId -> snake -> pets_pet_id
        assert tools[0].name == "get_pets_pet_id"
        assert any("operationId" in w for w in warnings)

    def test_collision_suffix(self):
        eps = [
            _ep(operation_id="listPets", path="/a"),
            _ep(operation_id="listPets", path="/b"),  # 同名不同接口
        ]
        tools, warnings = generate_tools(eps)
        assert [t.name for t in tools] == ["list_pets", "list_pets_2"]
        assert any("冲突" in w for w in warnings)

    def test_max_tools_exceeded(self):
        eps = [_ep(operation_id=f"op{i}", path=f"/x{i}") for i in range(5)]
        with pytest.raises(RegistrationError, match="上限"):
            generate_tools(eps, max_tools=4)


class TestInputSchema:
    def test_path_param_forced_required(self):
        ep = _ep(
            operation_id="getPetById",
            params=[ApiParam("petId", ParamLocation.path, required=False, schema={"type": "integer"})],
        )
        schema = build_input_schema(ep)
        assert schema["required"] == ["petId"]
        assert schema["properties"]["petId"]["type"] == "integer"
        assert schema["additionalProperties"] is False

    def test_query_optional(self):
        ep = _ep(
            operation_id="listPets",
            path="/pets",
            params=[
                ApiParam("limit", ParamLocation.query, required=False, schema={"type": "integer", "default": 20}),
                ApiParam("status", ParamLocation.query, required=True, schema={"type": "string", "enum": ["a", "b"]}),
            ],
        )
        schema = build_input_schema(ep)
        assert schema["required"] == ["status"]
        assert schema["properties"]["limit"]["type"] == "integer"
        assert schema["properties"]["status"]["enum"] == ["a", "b"]

    def test_body_property(self):
        ep = _ep(
            operation_id="createPet",
            method="post",
            path="/pets",
            body=ApiBody(schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, required=True),
        )
        schema = build_input_schema(ep)
        assert "body" in schema["properties"]
        assert schema["properties"]["body"]["type"] == "object"
        assert schema["required"] == ["body"]

    def test_body_optional(self):
        ep = _ep(method="post", path="/pets", body=ApiBody(schema={"type": "object"}, required=False))
        schema = build_input_schema(ep)
        assert "body" not in schema["required"]

    def test_noise_keys_removed(self):
        ep = _ep(
            operation_id="x",
            params=[
                ApiParam(
                    "p",
                    ParamLocation.query,
                    schema={"type": "string", "example": "e", "xml": {"name": "x"}, "readOnly": False},
                )
            ],
        )
        schema = build_input_schema(ep)
        assert "example" not in schema["properties"]["p"]
        assert "xml" not in schema["properties"]["p"]

    def test_description_built(self):
        ep = _ep(operation_id="getPetById", summary="按 ID 查询宠物", description="附加说明")
        tools, _ = generate_tools([ep])
        assert "按 ID 查询宠物" in tools[0].description
        assert "GET /pets/{petId}" in tools[0].description

    def test_deprecated_flag_in_description(self):
        ep = _ep(operation_id="old", deprecated=True)
        tools, _ = generate_tools([ep])
        assert "deprecated" in tools[0].description
