"""extractor：参数合并 / body 提取 / 过滤规则。"""

import pytest

from openapi_mcp_hub.errors import SpecError
from openapi_mcp_hub.extractor import extract_endpoints
from openapi_mcp_hub.loader import resolve_spec
from openapi_mcp_hub.models import Filters, ParamLocation


def _extract(spec, **filter_kwargs):
    return extract_endpoints(resolve_spec(spec), Filters(**filter_kwargs))


class TestParams:
    def test_path_level_and_op_level_merge(self, petstore_spec):
        # 注入 path 级参数，operation 级同名覆盖
        petstore_spec["paths"]["/pets"]["parameters"] = [
            {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}},
            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
        ]
        endpoints, _ = _extract(petstore_spec)
        list_pets = next(e for e in endpoints if e.operation_id == "listPets")
        by_name = {p.name: p for p in list_pets.params}
        # operation 级 limit（required=False）覆盖 path 级（required=True）
        assert by_name["limit"].required is False
        # path 级新增的 header 参数保留
        assert by_name["X-Trace"].location == ParamLocation.header

    def test_path_param_locations(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec)
        get_pet = next(e for e in endpoints if e.operation_id == "getPetById")
        assert get_pet.params[0].location == ParamLocation.path
        assert get_pet.params[0].required is True

    def test_body_extracted(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec)
        create = next(e for e in endpoints if e.operation_id == "createPet")
        assert create.body is not None
        assert create.body.required is True
        assert create.body.schema["required"] == ["name"]  # $ref 已内联

    def test_urlencoded_body_extracted_as_form(self, petstore_spec):
        """表单类 body 不再被忽略：字段提取，invoker 按表单提交（Unla form-data 对照采纳）。"""
        petstore_spec["paths"]["/pets"]["post"]["requestBody"]["content"] = {
            "application/x-www-form-urlencoded": {
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
            }
        }
        endpoints, warnings = _extract(petstore_spec)
        create = next(e for e in endpoints if e.operation_id == "createPet")
        assert create.body is not None
        assert create.body.media_type == "application/x-www-form-urlencoded"
        assert any("按表单字段提交" in w for w in warnings)

    def test_multipart_body_extracted(self, petstore_spec):
        petstore_spec["paths"]["/pets"]["post"]["requestBody"]["content"] = {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"photo": {"type": "string"}, "caption": {"type": "string"}},
                }
            }
        }
        endpoints, warnings = _extract(petstore_spec)
        create = next(e for e in endpoints if e.operation_id == "createPet")
        assert create.body is not None
        assert create.body.media_type == "multipart/form-data"

    def test_binary_body_still_ignored(self, petstore_spec):
        petstore_spec["paths"]["/pets"]["post"]["requestBody"]["content"] = {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
        }
        endpoints, warnings = _extract(petstore_spec)
        create = next(e for e in endpoints if e.operation_id == "createPet")
        assert create.body is None
        assert any("无 JSON content-type" in w for w in warnings)


class TestFilters:
    def test_include(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec, include=["/pets/{petId}"])
        assert {e.operation_id for e in endpoints} == {"getPetById", "deletePetById"}

    def test_exclude(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec, exclude=["/internal/*"])
        assert all("/internal" not in e.path for e in endpoints)

    def test_exclude_tags(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec, exclude_tags=["internal"])
        assert all("internalMetrics" != e.operation_id for e in endpoints)

    def test_deprecated_skipped_by_default(self, petstore_spec):
        petstore_spec["paths"]["/pets/{petId}"]["delete"]["deprecated"] = True
        endpoints, warnings = _extract(petstore_spec)
        assert all(e.operation_id != "deletePetById" for e in endpoints)
        assert any("deprecated" in w for w in warnings)

    def test_deprecated_kept_when_configured(self, petstore_spec):
        petstore_spec["paths"]["/pets/{petId}"]["delete"]["deprecated"] = True
        endpoints, _ = _extract(petstore_spec, include_deprecated=True)
        assert any(e.operation_id == "deletePetById" and e.deprecated for e in endpoints)

    def test_skip_head_options(self, petstore_spec):
        petstore_spec["paths"]["/pets"]["head"] = {"responses": {"200": {"description": "ok"}}}
        petstore_spec["paths"]["/pets"]["options"] = {"responses": {"200": {"description": "ok"}}}
        endpoints, warnings = _extract(petstore_spec)
        assert {e.method for e in endpoints} == {"get", "post", "delete"}
        assert any("HEAD" in w for w in warnings)

    def test_filter_to_empty_raises(self, petstore_spec):
        with pytest.raises(SpecError, match="没有任何可转换"):
            _extract(petstore_spec, include=["/nothing/*"])


class TestBasics:
    def test_all_petstore_endpoints(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec)
        assert {e.operation_id for e in endpoints} == {
            "listPets",
            "createPet",
            "getPetById",
            "deletePetById",
            "whoami",
            "internalMetrics",
        }

    def test_tags_carried(self, petstore_spec):
        endpoints, _ = _extract(petstore_spec)
        internal = next(e for e in endpoints if e.operation_id == "internalMetrics")
        assert internal.tags == ["internal"]
