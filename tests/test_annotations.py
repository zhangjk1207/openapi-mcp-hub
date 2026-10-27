"""MCP 工具标注推断测试（annotations.py）与端到端 list_tools 呈现。"""

from __future__ import annotations

import pytest

from openapi_mcp_hub.annotations import annotations_to_dict, infer_annotations

# ---------------------------------------------------------------- 单元：推断


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get", "Get"])
def test_read_only_methods(method):
    a = infer_annotations(method)
    assert a["readOnlyHint"] is True
    assert a["destructiveHint"] is False
    assert a["idempotentHint"] is True


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH"])
def test_destructive_methods(method):
    a = infer_annotations(method)
    assert a["readOnlyHint"] is False
    assert a["destructiveHint"] is True
    assert a["idempotentHint"] is (method in {"DELETE", "PUT"})


def test_post_create_is_additive():
    a = infer_annotations("POST", name="create_pet", path="/pets")
    assert a["destructiveHint"] is False
    assert a["idempotentHint"] is False


def test_post_with_destructive_verb_wins_over_additive():
    """POST + 危险动词：危险优先（保守）。"""
    a = infer_annotations("POST", name="reset_cache", path="/cache/reset")
    assert a["destructiveHint"] is True


def test_post_unknown_intent_defaults_to_destructive():
    """POST 语义不明时按 destructive（MCP 规范默认值也是 true）。"""
    a = infer_annotations("POST", name="pet", path="/pets")
    assert a["destructiveHint"] is True


@pytest.mark.parametrize(
    "name", ["delete_pet_by_id", "drop_table", "purge_logs", "remove_member", "truncate_all"]
)
def test_destructive_verb_detected_on_any_method(name):
    assert infer_annotations("POST", name=name)["destructiveHint"] is True


def test_closed_world_flag():
    assert infer_annotations("GET", closed_world=True)["openWorldHint"] is False
    assert infer_annotations("GET", closed_world=False)["openWorldHint"] is True


def test_annotations_to_dict_maps_field_names():
    a = infer_annotations("GET")
    kwargs = annotations_to_dict(a)
    assert set(kwargs) == {
        "read_only_hint",
        "destructive_hint",
        "idempotent_hint",
        "open_world_hint",
    }
    assert kwargs["read_only_hint"] is True


# ---------------------------------------------------------------- 端到端：list_tools


async def _list_tools(gateway, name: str, key: str) -> dict[str, dict]:
    resp = await gateway.post(
        f"/mcp/{name}",
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200, resp.text
    return {t["name"]: t for t in resp.json()["result"]["tools"]}


async def test_list_tools_exposes_annotations(gateway, petstore_spec):
    reg = await gateway.post(
        "/services",
        json={"name": "ann-svc", "spec": petstore_spec, "base_url": "http://target.test"},
    )
    assert reg.status_code == 201, reg.text
    key = reg.json()["api_key"]

    tools = await _list_tools(gateway, "ann-svc", key)
    assert tools, "应至少暴露一个工具"

    # 每个工具都必须带 annotations（客户端据此决定是否需要人工确认）
    for name, tool in tools.items():
        assert tool.get("annotations"), f"{name} 缺少 annotations"

    readonly = [n for n, t in tools.items() if t["annotations"]["readOnlyHint"]]
    assert readonly, "GET 类工具应被标为只读"
    # petstore 的删除接口必须是 destructive
    if "delete_pet_by_id" in tools:
        ann = tools["delete_pet_by_id"]["annotations"]
        assert ann["readOnlyHint"] is False
        assert ann["destructiveHint"] is True

    await gateway.delete("/services/ann-svc")


async def test_admin_detail_exposes_annotations(gateway, petstore_spec):
    reg = await gateway.post(
        "/services",
        json={"name": "ann-admin", "spec": petstore_spec, "base_url": "http://target.test"},
    )
    assert reg.status_code == 201
    detail = await gateway.get("/services/ann-admin")
    assert detail.status_code == 200
    for tool in detail.json()["tools"]:
        assert "annotations" in tool and tool["annotations"]
    await gateway.delete("/services/ann-admin")
