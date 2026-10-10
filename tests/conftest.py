"""共享 fixtures。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_to_mcp.config import GatewayConfig
from api_to_mcp.models import ServiceRegistration

REPO_ROOT = Path(__file__).resolve().parents[1]

PETSTORE_SPEC = json.loads((REPO_ROOT / "examples" / "petstore-openapi.json").read_text(encoding="utf-8"))


@pytest.fixture
def petstore_spec() -> dict:
    return json.loads(json.dumps(PETSTORE_SPEC))  # 深拷贝，防止测试间污染


@pytest.fixture
def gateway_cfg(tmp_path) -> GatewayConfig:
    return GatewayConfig(admin_token="test-admin-token", data_dir=tmp_path)


_UNSET = object()  # 哨兵：允许调用方显式传 spec=None（如 spec_text 用例），区别于"没传"


def make_registration(
    name: str = "petstore",
    spec: dict | None = _UNSET,
    base_url: str = "http://target.test",
    **overrides,
) -> ServiceRegistration:
    payload = {
        "name": name,
        "title": "测试服务",
        "spec": (json.loads(json.dumps(PETSTORE_SPEC)) if spec is _UNSET else spec),
        "base_url": base_url,
        "owner": "tester",
    }
    payload.update(overrides)
    return ServiceRegistration(**payload)


@pytest.fixture
async def gateway(gateway_cfg):
    """ASGI 内测网关客户端：默认带 admin token（数据面 /mcp 只看 X-Api-Key）。"""
    import httpx
    from httpx import ASGITransport

    from api_to_mcp.main import create_app

    app = create_app(gateway_cfg)
    await app.state.registry.connect_store()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": "Bearer test-admin-token"},
    ) as c:
        yield c
    await app.state.registry.shutdown()
