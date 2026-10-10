"""管理面：/services CRUD，Bearer admin token 鉴权。"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .config import GatewayConfig
from .errors import RegistrationError, ServiceConflictError, ServiceNotFoundError, SpecError
from .models import ServiceRegistration
from .registry import Registry

MAX_BODY_SPEC_BYTES = 12 * 1024 * 1024  # 注册请求体上限（spec 内嵌时）


def build_admin_router(registry: Registry, cfg: GatewayConfig) -> APIRouter:
    router = APIRouter(prefix="/services", tags=["admin"])

    def require_admin(request: Request) -> None:
        if not cfg.admin_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="管理面未配置 admin token（设置 API_TO_MCP_ADMIN_TOKEN 后重启）",
            )
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token, cfg.admin_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="管理面鉴权失败：需要 Authorization: Bearer <admin-token>",
            )

    def _http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ServiceConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ServiceNotFoundError):
            return HTTPException(status_code=404, detail=f"服务不存在: {exc}")
        if isinstance(exc, (SpecError, RegistrationError)):
            detail = {"message": str(exc)}
            if getattr(exc, "issues", None):
                detail["issues"] = exc.issues
            return HTTPException(status_code=422, detail=detail)
        return HTTPException(status_code=500, detail=str(exc))

    def _mcp_url(request: Request, name: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/mcp/{name}"

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_admin)],
        summary="注册服务：spec + base_url(+认证/过滤) => 即刻生成 MCP 端点",
    )
    async def create_service(reg: ServiceRegistration, request: Request) -> dict[str, Any]:
        if reg.spec is None and reg.spec_text is None and not reg.spec_url and reg.db is None:
            raise HTTPException(status_code=422, detail="spec / spec_text / spec_url / db 至少填一个（db 为数据库注册源）")
        try:
            svc, api_key, warnings = await registry.register(reg)
        except (SpecError, RegistrationError, ServiceConflictError, ServiceNotFoundError) as exc:
            raise _http_error(exc) from exc
        name = reg.name
        mcp_url = _mcp_url(request, name)
        return {
            "name": name,
            "title": svc.record.get("title"),
            "mcp_url": mcp_url,
            "api_key": api_key,  # 仅此一次返回，请妥善保存
            "tool_count": len(svc.tools),
            "tools": [t.name for t in svc.tools],
            "warnings": warnings,
            "claude_code_cmd": (
                f"claude mcp add --transport http {name} {mcp_url} --header 'X-Api-Key: {api_key}'"
            ),
        }

    @router.post(
        "/preview",
        dependencies=[Depends(require_admin)],
        summary="预览注册结果：完整走流水线但不落盘、不生成端点/key（template-tester 平替）",
    )
    async def preview_service(payload: dict[str, Any]) -> dict[str, Any]:
        sample = payload.pop("sample_response", None)
        reg = ServiceRegistration(**payload)
        if reg.spec is None and reg.spec_text is None and not reg.spec_url and reg.db is None:
            raise HTTPException(status_code=422, detail="spec / spec_text / spec_url / db 至少填一个（db 为数据库注册源）")
        try:
            return await registry.preview(reg, sample_response=sample)
        except Exception as exc:  # noqa: BLE001 统一映射为 4xx/5xx
            raise _http_error(exc) from exc

    @router.get("", dependencies=[Depends(require_admin)], summary="服务列表")
    async def list_services() -> list[dict[str, Any]]:
        return registry.list_services()

    @router.get("/{name}", dependencies=[Depends(require_admin)], summary="服务详情")
    async def get_service(name: str) -> dict[str, Any]:
        try:
            svc = registry.get(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc
        return {
            **svc.summary(),
            "response_template": svc.record.get("response_template"),
            "tool_response_templates": svc.record.get("tool_response_templates", {}),
            "allowed_tools": svc.record.get("allowed_tools") or None,
            "tools": [
                {
                    "name": t.name,
                    "description": (t.description or "").split("\n")[0],
                    "required": t.input_schema.get("required", []),
                    "response_template": t.response_template,
                }
                for t in svc.tools
            ],
        }

    @router.get("/{name}/tools", dependencies=[Depends(require_admin)], summary="工具清单（完整定义，预览用）")
    async def get_service_tools(name: str) -> list[dict[str, Any]]:
        try:
            svc = registry.get(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc
        return [t.to_public_dict() for t in svc.tools]

    @router.get("/{name}/health", dependencies=[Depends(require_admin)], summary="目标 API 健康探测（HEAD/GET + 凭证 env 自检，不回传凭证值）")
    async def get_service_health(name: str) -> dict[str, Any]:
        try:
            self_check = await registry.health(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc
        return self_check

    @router.get("/{name}/calls", dependencies=[Depends(require_admin)], summary="近期工具调用日志（内存环形缓冲，重启清零）")
    async def get_service_calls(name: str, limit: int = 50) -> dict[str, Any]:
        try:
            svc = registry.get(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc
        calls = list(reversed(svc.app.calls))[: max(1, min(limit, 200))]
        return {"service": name, "count": len(calls), "calls": calls}

    @router.put("/{name}", dependencies=[Depends(require_admin)], summary="更新服务（旧 MCP session 全部作废）")
    async def update_service(name: str, reg: ServiceRegistration) -> dict[str, Any]:
        try:
            svc, warnings = await registry.update(name, reg)
        except (SpecError, RegistrationError, ServiceConflictError, ServiceNotFoundError) as exc:
            raise _http_error(exc) from exc
        return {**svc.summary(), "warnings": warnings}

    @router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)], summary="下线服务")
    async def delete_service(name: str) -> None:
        try:
            await registry.remove(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc

    @router.post("/{name}/rotate-key", dependencies=[Depends(require_admin)], summary="轮换数据面 key")
    async def rotate_key(name: str) -> dict[str, str]:
        try:
            api_key = await registry.rotate_key(name)
        except ServiceNotFoundError as exc:
            raise _http_error(exc) from exc
        return {"name": name, "api_key": api_key}

    return router
