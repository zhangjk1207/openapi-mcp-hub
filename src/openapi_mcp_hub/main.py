"""FastAPI 应用工厂与启动入口。

启动恢复：lifespan 内连接持久化后端（pg 或文件）并 load_from_store() 全量重建，
agent 侧 URL 不变。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin import build_admin_router
from .config import GatewayConfig
from .mcp_host import McpHost
from .registry import Registry
from .store import FileStore, PgStore, Store

logger = logging.getLogger(__name__)


def create_app(cfg: GatewayConfig | None = None) -> FastAPI:
    cfg = cfg or GatewayConfig.from_env()
    # 持久化后端：配了 database_url 用 pg（文件目录作为首次迁移源），否则文件
    store: Store
    if cfg.database_url:
        store = PgStore(cfg.database_url, legacy_services_dir=cfg.data_dir / "services")
    else:
        store = FileStore(cfg.data_dir / "services")
    host = McpHost()
    registry = Registry(cfg, host, store=store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await registry.connect_store()  # pg 建表/自动迁移；失败则启动退出（fail fast）
        restored = await registry.load_from_store()
        logger.info("openapi-mcp-hub %s 就绪（恢复 %d 个服务）", __version__, len(restored))
        yield
        await registry.shutdown()
        await registry.close_store()

    app = FastAPI(
        title="openapi-mcp-hub",
        version=__version__,
        description="OpenAPI → MCP 网关：POST /services 注册 spec，即刻生成 /mcp/{name} 端点",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.registry = registry
    app.state.mcp_host = host

    app.include_router(build_admin_router(registry, cfg))
    # 数据面：/mcp/{name}，ASGI 直接分发给对应服务的 MCP 实例
    app.mount("/mcp", host)


    @app.get("/healthz", summary="健康检查")
    async def healthz() -> dict:
        return {"status": "ok"}

    # 轻量控制台（单文件静态页，零构建链）
    # ⚠ 必须是 create_app 最后一个挂载：静态 mount 在 "/" 会按注册顺序兜底一切
    # 未匹配路径——之后新增的 @app.get 路由将永远 404（测试 test_ui 会拦住这类回归）
    _ui_dir = Path(__file__).parent / "static"
    if _ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(_ui_dir), html=True), name="ui")
    else:  # 无 static 目录（如裁剪部署）时保留 JSON 根路由
        @app.get("/", summary="网关信息")
        async def root() -> dict:
            return {
                "service": "openapi-mcp-hub",
                "version": __version__,
                "endpoints": {
                    "admin": "/services（Bearer admin token）",
                    "mcp": "/mcp/{service-name}（X-Api-Key）",
                    "health": "/healthz",
                    "docs": "/docs",
                },
                "services": host.names(),
            }

    return app


def main() -> None:  # console_script 入口：openapi-mcp-hub
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    cfg = GatewayConfig.from_env()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


app = create_app()
