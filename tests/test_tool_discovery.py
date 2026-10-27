"""词法工具检索（progressive tool discovery）的单元与端到端测试。"""

from __future__ import annotations

import json

from openapi_mcp_hub.search import ToolIndex, tokenize

# ---------------------------------------------------------------- 分词


def test_tokenize_ascii_drops_stopwords_and_single_chars():
    toks = tokenize("Get Pet By ID: returns a pet x")
    assert "pet" in toks
    assert "id" in toks  # 两个字母的参数名是有意义的
    assert "returns" not in toks  # 停用词
    assert "a" not in toks
    assert "x" not in toks  # 单字符无区分度


def test_tokenize_cjk_uses_bigrams():
    toks = tokenize("查询订单状态")
    assert toks == ["查询", "询订", "订单", "单状", "状态"]


def test_tokenize_mixed_language():
    toks = tokenize("create_pet 创建宠物")
    assert "create" in toks
    assert "创建" in toks


# ---------------------------------------------------------------- 打分


def _index() -> ToolIndex:
    return ToolIndex(
        [
            ("list_pets", "list_pets GET /pets 列出所有宠物 limit status", "列出所有宠物"),
            ("create_pet", "create_pet POST /pets 创建一只宠物 name tag", "创建一只宠物"),
            ("delete_pet_by_id", "delete_pet_by_id DELETE /pets/{petId} 删除宠物 petId", "删除宠物"),
            ("get_store_inventory", "get_store_inventory GET /store/inventory 库存统计", "库存统计"),
        ]
    )


def test_search_ranks_matching_tool_first():
    hits = _index().search("删除宠物")
    assert hits
    assert hits[0][0] == "delete_pet_by_id"


def test_search_matches_param_name():
    """用户说"按 petId 删"时应该命中 petId 那个工具。"""
    hits = _index().search("petId")
    assert hits[0][0] == "delete_pet_by_id"


def test_search_no_match_returns_empty():
    assert _index().search("zzz-nonexistent-zzz") == []


def test_search_empty_query_returns_empty():
    assert _index().search("") == []
    assert _index().search("   ") == []


def test_search_is_deterministic():
    """同分时按名字升序，保证顺序稳定（利于 prompt cache）。"""
    first = _index().search("宠物")
    for _ in range(5):
        assert _index().search("宠物") == first


def test_search_respects_limit():
    hits = _index().search("宠物", limit=1)
    assert len(hits) == 1


def test_index_len_and_summary():
    idx = _index()
    assert len(idx) == 4
    assert idx.summary("list_pets") == "列出所有宠物"
    assert idx.summary("nope") == ""


# ---------------------------------------------------------------- 端到端


def _many_path_spec(n: int = 60) -> dict:
    """n 个 GET 端点 + n 个 DELETE 端点？只要 n 个工具即可。"""
    paths = {}
    for i in range(n):
        paths[f"/resource{i}"] = {
            "get": {
                "operationId": f"get_resource_{i}",
                "summary": f"获取资源 {i}",
                "description": f"查询第 {i} 号资源的详情",
                "responses": {"200": {"description": "ok"}},
            }
        }
    return {"openapi": "3.0.3", "info": {"title": "many", "version": "1"}, "paths": paths}


async def _list(gateway, name: str, key: str) -> tuple[dict, list[dict]]:
    resp = await gateway.post(
        f"/mcp/{name}",
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return result, result["tools"]


async def _call(gateway, name: str, key: str, tool: str, args: dict) -> dict:
    resp = await gateway.post(
        f"/mcp/{name}",
        headers={"X-Api-Key": key, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


async def test_small_service_has_no_search_tool(gateway, petstore_spec):
    """工具少时不该多出一个元工具（petstore 6 个）。"""
    reg = await gateway.post(
        "/services", json={"name": "small-svc", "spec": petstore_spec, "base_url": "http://t.test"}
    )
    result, tools = await _list(gateway, "small-svc", reg.json()["api_key"])
    assert "search_tools" not in {t["name"] for t in tools}


# 2026-07-28 协议要求 _meta 携带协议版本与客户端能力；缺了按旧版处理，
# 旧版响应里没有 ttlMs/cacheScope/resultType。
_META_2026 = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


async def _list_modern(gateway, name: str, key: str) -> dict:
    resp = await gateway.post(
        f"/mcp/{name}",
        headers={
            "X-Api-Key": key,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": _META_2026},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


async def test_tools_list_declares_cache_hint(gateway, petstore_spec):
    """规范鼓励服务端声明 list 结果可缓存，提升 prompt cache 命中率。"""
    reg = await gateway.post(
        "/services", json={"name": "cache-svc", "spec": petstore_spec, "base_url": "http://t.test"}
    )
    result = await _list_modern(gateway, "cache-svc", reg.json()["api_key"])
    assert result["ttlMs"] == 300_000
    assert result["cacheScope"] == "public"
    assert result["resultType"] == "complete"


async def test_tools_list_order_is_readonly_first_then_name(gateway, petstore_spec):
    reg = await gateway.post(
        "/services", json={"name": "ord-svc", "spec": petstore_spec, "base_url": "http://t.test"}
    )
    _, tools = await _list(gateway, "ord-svc", reg.json()["api_key"])
    readonly = [t["annotations"]["readOnlyHint"] for t in tools]
    assert readonly == sorted(readonly, reverse=True)  # 只读排在前
    groups: dict[bool, list[str]] = {}
    for t in tools:
        groups.setdefault(t["annotations"]["readOnlyHint"], []).append(t["name"])
    for names in groups.values():
        assert names == sorted(names)


async def test_large_service_truncates_and_offers_search(gateway):
    """60 个工具、上限 50：只列 49 + search_tools，描述里说明还有多少没列。"""
    reg = await gateway.post(
        "/services",
        json={"name": "big-svc", "spec": _many_path_spec(60), "base_url": "http://t.test"},
    )
    assert reg.status_code == 201, reg.text
    _, tools = await _list(gateway, "big-svc", reg.json()["api_key"])
    assert len(tools) == 50
    assert tools[0]["name"] == "search_tools"
    assert "另有 11 个" in tools[0]["description"]


async def test_search_finds_unlisted_tool(gateway):
    """被截断掉的工具必须能通过 search_tools 找到。"""
    reg = await gateway.post(
        "/services",
        json={"name": "disc-svc", "spec": _many_path_spec(60), "base_url": "http://t.test"},
    )
    key = reg.json()["api_key"]
    _, tools = await _list(gateway, "disc-svc", key)
    listed = {t["name"] for t in tools}
    all_names = {f"get_resource_{i}" for i in range(60)}
    hidden = all_names - listed
    assert hidden

    target = sorted(hidden)[0]
    num = target.rsplit("_", 1)[1]
    result = await _call(gateway, "disc-svc", key, "search_tools", {"query": f"资源 {num}"})
    text = result["content"][0]["text"]
    assert target in text


async def test_search_tool_is_annotated_readonly(gateway):
    reg = await gateway.post(
        "/services",
        json={"name": "ann-disc", "spec": _many_path_spec(10), "base_url": "http://t.test"},
    )
    _, tools = await _list(gateway, "ann-disc", reg.json()["api_key"])
    search = next(t for t in tools if t["name"] == "search_tools")
    assert search["annotations"]["readOnlyHint"] is True


async def test_search_rejects_empty_query(gateway):
    reg = await gateway.post(
        "/services",
        json={"name": "empty-q", "spec": _many_path_spec(10), "base_url": "http://t.test"},
    )
    result = await _call(gateway, "empty-q", reg.json()["api_key"], "search_tools", {"query": ""})
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"]["code"] == -32602


async def test_search_tool_name_avoids_collision(gateway):
    """真实 API 里已经有 search_tools 时，元工具让位改名。"""
    spec = _many_path_spec(9)
    spec["paths"]["/search"] = {
        "get": {
            "operationId": "search_tools",
            "summary": "搜索工具",
            "responses": {"200": {"description": "ok"}},
        }
    }
    reg = await gateway.post(
        "/services", json={"name": "collide", "spec": spec, "base_url": "http://t.test"}
    )
    _, tools = await _list(gateway, "collide", reg.json()["api_key"])
    names = {t["name"] for t in tools}
    assert "hub_search_tools" in names
    assert "search_tools" in names  # 真实端点保留原名


async def test_advertise_limit_zero_disables_truncation(gateway_cfg):
    """--advertise-limit 0 时不截断，但仍提供 search_tools（工具数 >= 8）。"""
    import httpx
    from httpx import ASGITransport

    from openapi_mcp_hub.main import create_app

    gateway_cfg.advertise_limit = 0
    app = create_app(gateway_cfg)
    await app.state.registry.connect_store()
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            client.headers["Authorization"] = "Bearer test-admin-token"
            reg = await client.post(
                "/services",
                json={"name": "nolimit", "spec": _many_path_spec(60), "base_url": "http://t.test"},
            )
            assert reg.status_code == 201, reg.text
            _, tools = await _list(client, "nolimit", reg.json()["api_key"])
            assert len(tools) == 61  # 60 个工具 + search_tools
            search = tools[0]
            assert search["name"] == "search_tools"
            assert "另有" not in search["description"]  # 没截断就不该说"另有 N 个"
    finally:
        await app.state.registry.shutdown()
