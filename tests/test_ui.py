"""轻量控制台（单文件静态页）测试。"""

import httpx
from httpx import ASGITransport

from tests.conftest import PETSTORE_SPEC


async def test_ui_served_at_root(gateway):
    """GET / 返回控制台 HTML（API 路由优先注册，不受静态挂载影响）。"""
    resp = await gateway.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "openapi-mcp-hub · 控制台" in resp.text
    # 无外部依赖：不引任何外链资源（内网可用性）
    assert 'src="http' not in resp.text and 'href="http' not in resp.text


async def test_api_routes_not_shadowed(gateway):
    """静态挂载在 / 之后：API 路径仍走 API（健康/管理面不被静态页吞掉）。"""
    assert (await gateway.get("/healthz")).status_code == 200
    # 未带 token 的管理面仍 401（不被静态页吞掉）

    raw = httpx.AsyncClient(transport=ASGITransport(app=gateway._transport.app), base_url="http://t")
    resp = await raw.get("/services")
    assert resp.status_code == 401
    await raw.aclose()


async def test_docs_disabled_by_default(gateway):
    """管理面 schema 默认不对外暴露（减重：控制台已覆盖全部操作）。"""
    assert (await gateway.get("/openapi.json")).status_code == 404
    assert (await gateway.get("/docs")).status_code != 200


async def test_docs_opt_in(gateway_cfg):
    """OPENAPI_MCP_HUB_DOCS=1（即 docs_enabled）时 /docs 与 /openapi.json 可用。"""
    from openapi_mcp_hub.main import create_app

    gateway_cfg.docs_enabled = True
    app = create_app(gateway_cfg)
    await app.state.registry.connect_store()
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as raw:
            assert (await raw.get("/openapi.json")).status_code == 200
            assert (await raw.get("/docs")).status_code == 200
    finally:
        await app.state.registry.shutdown()


async def test_ui_present_after_registration(gateway):
    """注册一个服务后根路径仍是 HTML（顺序稳定）。"""
    resp = await gateway.post(
        "/services",
        json={"name": "ui-svc", "spec": PETSTORE_SPEC, "base_url": "http://target.test"},
    )
    assert resp.status_code == 201
    root = await gateway.get("/")
    assert root.headers["content-type"].startswith("text/html")
    await gateway.delete("/services/ui-svc")
