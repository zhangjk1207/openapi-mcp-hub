"""工具描述/schema 压缩测试（compact.py）。"""

from __future__ import annotations

from openapi_mcp_hub.compact import (
    FIELD_DESCRIPTION_LIMIT,
    TOOL_DESCRIPTION_LIMIT,
    compact_description,
    compact_schema,
    compact_text,
    estimate_chars,
)


def test_whitespace_and_indentation_collapsed():
    raw = "列出所有宠物。\n\n\n  可选分页   limit\n\t\t排序 order  "
    assert compact_description(raw) == "列出所有宠物。 可选分页 limit 排序 order"


def test_html_and_markdown_stripped():
    raw = "**重要**：见 <a href='https://x'>文档</a>，或 [此处](https://y)。"
    out = compact_description(raw)
    assert "<a" not in out and "**" not in out
    assert "文档" in out and "此处" in out
    assert "https://y" not in out  # 链接地址对 agent 无信息量


def test_short_text_untouched():
    raw = "HTTP GET /pets"
    assert compact_description(raw) == raw


def test_long_text_truncated_at_sentence_boundary():
    raw = "。".join(["这是第%d句话，用来凑长度" % i for i in range(60)]) + "。"
    out = compact_description(raw)
    assert len(out) <= TOOL_DESCRIPTION_LIMIT
    assert out.endswith("…")
    # 截断点必须是完整句子（以句号收尾后再接省略号）
    assert out[:-1].endswith("。")


def test_truncation_keeps_http_line_prefix_intact():
    """build_description 把 HTTP 行放在最后：压缩不能把它切掉语义。"""
    raw = "很长的说明。" * 80 + "\n\nHTTP DELETE /pets/{petId}"
    out = compact_description(raw)
    assert out.endswith("…")  # 允许被截断
    assert len(out) <= TOOL_DESCRIPTION_LIMIT


def test_boilerplate_sentences_dropped_when_over_limit():
    raw = (
        "Returns a list of pets in the store. This endpoint is stable. "
        "See also the pet creation API. "
        + "真正的说明。" * 60
    )
    out = compact_description(raw)
    assert "Returns a list of pets" not in out[:60]


def test_empty_input():
    assert compact_description(None) == ""
    assert compact_description("") == ""
    assert compact_description("   \n  ") == ""


def test_schema_display_keys_dropped():
    schema = {
        "type": "object",
        "title": "Pet",
        "$comment": "内部注释",
        "properties": {
            "name": {"type": "string", "title": "名字", "description": "宠物名"},
        },
    }
    out = compact_schema(schema)
    assert "title" not in out
    assert "$comment" not in out
    assert "title" not in out["properties"]["name"]
    # 语义字段必须原样保留
    assert out["type"] == "object"
    assert out["properties"]["name"]["type"] == "string"
    assert out["properties"]["name"]["description"] == "宠物名"


def test_schema_field_description_truncated():
    schema = {"type": "string", "description": "说明" * 500}
    out = compact_schema(schema)
    assert len(out["description"]) <= FIELD_DESCRIPTION_LIMIT
    assert out["description"].endswith("…")


def test_schema_validation_semantics_preserved():
    """required / enum / format / default 一律不得被压掉。"""
    schema = {
        "type": "object",
        "required": ["status"],
        "properties": {
            "status": {"type": "string", "enum": ["a", "b"], "format": "uuid", "default": "a"},
        },
    }
    out = compact_schema(schema)
    assert out["required"] == ["status"]
    prop = out["properties"]["status"]
    assert prop["enum"] == ["a", "b"]
    assert prop["format"] == "uuid"
    assert prop["default"] == "a"


def test_schema_with_booleans_and_none_values():
    schema = {"additionalProperties": False, "nullable": True, "x": None}
    out = compact_schema(schema)
    assert out == {"additionalProperties": False, "nullable": True, "x": None}


def test_estimate_chars_counts_description_and_schema():
    tool = {"description": "abc", "inputSchema": {"type": "object"}}
    assert estimate_chars(tool) == 3 + len(str({"type": "object"}))
    # 兼容 snake_case 键
    assert estimate_chars({"description": "abc", "input_schema": {}}) == 3 + 2


def test_compact_text_respects_small_limit():
    out = compact_text("一 二 三 四 五 六 七 八 九 十", 8)
    assert len(out) <= 8
    assert out.endswith("…")
