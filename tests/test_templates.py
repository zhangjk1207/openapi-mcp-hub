"""响应模板引擎（templates.py）单测：渲染语义 + 注册期校验。"""

from __future__ import annotations

import json

import pytest

from openapi_mcp_hub.templates import (
    TemplateError,
    render_response,
    validate_template,
)


class TestValidate:
    def test_ok(self):
        validate_template("宠物 {{response.name}}（id={{response.id}}，HTTP {{status}}）")
        validate_template("{{response}}")
        validate_template("{{response.a.b.0.c}} 调用工具 {{tool}} {{ok}}")
        validate_template("{{response.owner.name || 未登记}}")  # || 默认值

    def test_fallback_may_contain_spaces_and_unicode(self):
        validate_template("主人：{{response.owner || 未知用户，请稍后重试}}")

    def test_empty(self):
        with pytest.raises(TemplateError):
            validate_template("   ")

    def test_unbalanced(self):
        with pytest.raises(TemplateError, match="配平"):
            validate_template("{{response.name}")
        with pytest.raises(TemplateError, match="配平"):
            validate_template("{{response.name}}}}")

    def test_unknown_root(self):
        with pytest.raises(TemplateError, match="不支持的模板变量"):
            validate_template("{{foo.bar}}")

    def test_unparsable_placeholder(self):
        with pytest.raises(TemplateError, match="无法解析"):
            validate_template("{{response-a-b}}")


class TestRender:
    RESP = {
        "id": 42,
        "name": "小白",
        "status": "available",
        "tags": [{"name": "dog"}, {"name": "pet"}],
        "owner": None,
        "meta": {"nested": {"deep": "v"}},
    }

    def _render(self, template: str) -> str:
        return render_response(template, response=self.RESP, status=200, tool="get_pet_by_id")

    def test_plain_path(self):
        assert self._render("宠物 {{response.name}}（id={{response.id}}）") == "宠物 小白（id=42）"

    def test_status_and_tool(self):
        assert self._render("{{tool}} => {{status}}") == "get_pet_by_id => 200"

    def test_ok_always_true(self):
        assert self._render("{{ok}}") == "true"

    def test_list_index(self):
        assert self._render("{{response.tags.0.name}}/{{response.tags.1.name}}") == "dog/pet"

    def test_whole_response_dict_compact_json(self):
        out = self._render("{{response}}")
        assert out == json.dumps(self.RESP, ensure_ascii=False, separators=(",", ":"))

    def test_missing_path_empty_string(self):
        assert self._render("[{{response.not_exist}}]") == "[]"
        assert self._render("[{{response.tags.99.name}}]") == "[]"
        assert self._render("[{{response.name.sub}}]") == "[]"  # str 上继续下钻
        assert self._render("[{{response.a.b.c.d}}]") == "[]"

    def test_fallback_used_when_missing(self):
        """|| 默认值（Unla safeGetOr 语义）：取值为空/缺失时启用，非空时忽略。"""
        assert self._render("主人：{{response.owner.name || 未登记}}") == "主人：未登记"
        assert self._render("{{response.owner || 佚名}}") == "佚名"

    def test_fallback_ignored_when_value_present(self):
        assert self._render("主人：{{response.name || 未登记}}") == "主人：小白"

    def test_fallback_zero_value_is_present(self):
        # "0" 是非空字符串，不算缺失
        out = render_response("[{{response.count || 无}}]", response={"count": 0}, status=200, tool="t")
        assert out == "[0]"

    def test_fallback_text_trimmed(self):
        assert self._render("[{{response.x ||  默认值  }}]") == "[默认值]"

    def test_none_renders_empty(self):
        assert self._render("[{{response.owner}}]") == "[]"

    def test_nested_deep(self):
        assert self._render("{{response.meta.nested.deep}}") == "v"

    def test_list_whole(self):
        out = render_response("{{response}}", response=[1, {"a": 2}], status=200, tool="t")
        assert out == '[1,{"a":2}]'

    def test_string_response(self):
        out = render_response("raw: {{response}}", response="plain text", status=200, tool="t")
        assert out == "raw: plain text"

    def test_never_raises_on_weird_payload(self):
        # 任意怪异 payload 都不抛错（safeGet 语义）
        out = render_response("{{response.x.y}}", response=12345, status=200, tool="t")
        assert out == ""
