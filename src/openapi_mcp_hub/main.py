"""FastAPI 应用工厂与启动入口。

启动恢复：lifespan 内连接持久化后端（pg 或文件）并 load_from_store() 全量重建，
agent 侧 URL 不变。
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin import build_admin_router
from .config import GatewayConfig
from .errors import ServiceNotFoundError
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
        from .demo import maybe_register_demo
        await maybe_register_demo(registry)
        logger.info("openapi-mcp-hub %s 就绪（恢复 %d 个服务）", __version__, len(restored))
        yield
        await registry.shutdown()
        await registry.close_store()

    app = FastAPI(
        title="openapi-mcp-hub",
        version=__version__,
        description="OpenAPI → MCP 网关：POST /services 注册 spec，即刻生成 /mcp/{name} 端点",
        lifespan=lifespan,
        # 默认不暴露管理面 schema（见 GatewayConfig.docs_enabled）
        docs_url="/docs" if cfg.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if cfg.docs_enabled else None,
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

    # RFC 9728 受保护资源元数据（MCP OAuth 2.1 客户端发现授权服务器的入口）。
    # ⚠ 必须注册在静态 mount("/") 之前（详见下方注释）；仅 jwt 外部 IdP 模式的服务
    # 存在此元数据——内置签发模式没有授权服务器可发现，token 由管理面签发。
    @app.get(
        "/.well-known/oauth-protected-resource/mcp/{name}",
        summary="RFC 9728 受保护资源元数据（jwt 外部 IdP 模式服务）",
    )
    async def protected_resource_metadata(name: str, request: Request) -> dict:
        try:
            svc = registry.get(name)
        except ServiceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        authn = svc.record.get("authn") or {}
        if authn.get("mode") != "jwt" or not authn.get("issuer"):
            raise HTTPException(
                status_code=404,
                detail=f"服务 {name} 未启用 jwt 外部 IdP 模式（无授权服务器可发现）",
            )
        return {
            "resource": f"{str(request.base_url).rstrip('/')}/mcp/{name}",
            "authorization_servers": [authn["issuer"]],
        }

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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openapi-mcp-hub",
        description=(
            "把 OpenAPI spec 或 PostgreSQL/MySQL 数据库变成 MCP 端点。"
            "所有参数均可选用；不传则读环境变量 OPENAPI_MCP_HUB_*（见 README）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  openapi-mcp-hub --port 8080 --data-dir ./data\n"
            "  OPENAPI_MCP_HUB_ADMIN_TOKEN=xxx openapi-mcp-hub --print-config\n"
        ),
    )
    p.add_argument("--version", action="store_true", help="打印版本号后退出")
    p.add_argument("--host", help="监听地址（默认 0.0.0.0，环境变量 OPENAPI_MCP_HUB_HOST）")
    p.add_argument("--port", type=int, help="监听端口（默认 20033，环境变量 OPENAPI_MCP_HUB_PORT）")
    p.add_argument(
        "--data-dir",
        help="文件后端目录（默认 ./data，环境变量 OPENAPI_MCP_HUB_DATA_DIR）",
    )
    p.add_argument(
        "--database-url",
        help="PostgreSQL DSN；不传则用文件后端（环境变量 OPENAPI_MCP_HUB_DATABASE_URL）",
    )
    p.add_argument(
        "--host-allowlist",
        help="目标主机白名单，逗号分隔（SSRF 防护，环境变量 OPENAPI_MCP_HUB_HOST_ALLOWLIST）",
    )
    p.add_argument(
        "--docs",
        action="store_true",
        help="打开 /docs 与 /openapi.json（默认关闭）",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别（默认 info）",
    )
    p.add_argument(
        "--advertise-limit",
        type=int,
        help="tools/list 一次列出的工具数上限，超出部分由 search_tools 检索（默认 50，0 = 不限制）",
    )
    p.add_argument(
        "--print-config",
        action="store_true",
        help="打印生效配置（凭证脱敏）后退出，用于排查启动问题",
    )
    return p


def _apply_cli_overrides(cfg: GatewayConfig, args: argparse.Namespace) -> GatewayConfig:
    """CLI 优先级高于环境变量（显式传参才覆盖）。"""
    if args.host:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.host_allowlist is not None:
        cfg.host_allowlist = [x.strip() for x in args.host_allowlist.split(",") if x.strip()]
    if args.docs:
        cfg.docs_enabled = True
    if args.advertise_limit is not None:
        cfg.advertise_limit = max(0, args.advertise_limit)
    return cfg


def _describe_config(cfg: GatewayConfig) -> str:
    """生效配置概览；凭证只报是否已配置，绝不回显值。"""
    lines = [
        f"admin_token      : {'已配置' if cfg.admin_token else '未配置（管理面不可用）'}",
        f"listen           : {cfg.host}:{cfg.port}",
        f"data_dir         : {cfg.data_dir.resolve()}",
        f"store            : {cfg.database_url and 'PostgreSQL' or 'file'}",
        f"host_allowlist   : {', '.join(cfg.host_allowlist) or '不限制（内网默认）'}",
        f"docs             : {'on' if cfg.docs_enabled else 'off'}",
        f"max_tools/service: {cfg.default_max_tools}",
        f"advertise_limit  : {cfg.advertise_limit or '不限制'}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:  # console_script 入口：openapi-mcp-hub
    import sys

    import uvicorn

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"openapi-mcp-hub {__version__}")
        return

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    cfg = _apply_cli_overrides(GatewayConfig.from_env(), args)

    if args.print_config:
        print(_describe_config(cfg))
        return

    if not cfg.admin_token:
        print(
            "警告：未设置 OPENAPI_MCP_HUB_ADMIN_TOKEN，管理面（注册/注销/轮换 key）不可用，"
            "只能通过环境变量 OPENAPI_MCP_HUB_DEMO=1 或预置 data/services/*.json 使用。",
            file=sys.stderr,
        )

    uvicorn.run(
        create_app(cfg), host=cfg.host, port=cfg.port, log_level=args.log_level
    )


app = create_app()
