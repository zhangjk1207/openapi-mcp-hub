"""演示模式：OPENAPI_MCP_HUB_DEMO=1 时启动后自动注册 petstore 演示服务。

仅供本地体验（docker compose up 打开浏览器即有可玩的服务）：
- 固定 api_key（README 公开）让演示文档可复制粘贴，生产请勿开启本模式；
- 幂等：服务已存在（重启恢复）时跳过，不会覆盖已有配置。
"""

from __future__ import annotations

import logging
import os
from importlib.resources import files

from .errors import ServiceNotFoundError
from .models import ServiceRegistration

logger = logging.getLogger(__name__)

DEMO_KEY = "psk-demo-petstore-key"
_DEMO_SERVICE = "petstore"
_ON = ("1", "true", "yes")


def demo_enabled() -> bool:
    return os.environ.get("OPENAPI_MCP_HUB_DEMO", "").strip().lower() in _ON


async def maybe_register_demo(registry) -> None:
    """demo 模式下确保演示服务存在（缺失则用固定 key 注册）。"""
    if not demo_enabled():
        return
    try:
        registry.get(_DEMO_SERVICE)
        return  # 已存在（重启恢复）→ 幂等跳过
    except ServiceNotFoundError:
        pass

    spec_text = files("openapi_mcp_hub").joinpath("demo_spec.json").read_text(encoding="utf-8")
    reg = ServiceRegistration(
        name=_DEMO_SERVICE,
        title="Petstore 演示",
        spec_text=spec_text,
        base_url=os.environ.get("OPENAPI_MCP_HUB_DEMO_API_URL", "http://petstore-mock:20034"),
        auth={"type": "bearer", "token_env": "DEMO_PETSTORE_TOKEN"},
    )
    _, plain, _ = await registry.register(reg, key=DEMO_KEY)
    logger.info("演示模式：已注册 %s（固定演示 key，见 README；生产勿开 DEMO）", _DEMO_SERVICE)
    assert plain == DEMO_KEY
