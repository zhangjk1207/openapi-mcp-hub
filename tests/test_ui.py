"""轻量控制台（单文件静态页）测试。"""

import httpx
from httpx import ASGITransport

from tests.conftest import PETSTORE_SPEC


async def test_ui_served_at_root(gateway):
    """GET / 返回控制台 HTML（API 路由优先注册，不受静态挂载影响）。"""
    resp = await gateway.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "api-to-mcp · 控制台" in resp.text
    # 无外部依赖：不引任何外链资源（内网可用性）
    assert 'src="http' not in resp.text and 'href="http' not in resp.text


async def test_api_routes_not_shadowed(gateway):
    """静态挂载在 / 之后：API 路径仍走 API（列表/健康/docs）。"""
    assert (await gateway.get("/healthz")).status_code == 200
    # 未带 token 的管理面仍 401（不被静态页吞掉）
    import httpx as _hx

    raw = httpx.AsyncClient(transport=ASGITransport(app=gateway._transport.app), base_url="http://t")
    resp = await raw.get("/services")
    assert resp.status_code == 401
    await raw.aclose()
    assert (await gateway.get("/openapi.json")).status_code == 200


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
