"""服务注册表：注册/更新/下线/轮换 key + 原子落盘 + 启动恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any

import httpx

from .config import GatewayConfig, TargetNotAllowed
from .db import DbExecutor, dsn_safe_display
from .errors import RegistrationError, ServiceConflictError, ServiceNotFoundError, SpecError
from .extractor import extract_endpoints
from .generator import generate_tools
from .invoker import Invoker, ServiceHttpConfig, apply_auth
from .loader import load_spec, normalize_spec, parse_spec_text
from .mcp_host import McpHost, McpServiceApp
from .models import AuthConfig, Filters, ServiceRegistration, ToolDef
from .store import FileStore, Store
from .templates import TemplateError, render_response, validate_template

logger = logging.getLogger(__name__)

_KEY_PREFIX = "psk-"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_api_key() -> tuple[str, str]:
    """返回 (明文 key, sha256 哈希)。明文仅在注册/轮换响应中出现一次。"""
    plain = _KEY_PREFIX + secrets.token_urlsafe(24)
    return plain, hashlib.sha256(plain.encode()).hexdigest()


@dataclass
class RunningService:
    record: dict[str, Any]
    tools: list[ToolDef]
    app: McpServiceApp
    invoker: Invoker | None = None          # REST 源
    db_executor: DbExecutor | None = None  # DB 源（M5）

    def summary(self) -> dict[str, Any]:
        db_cfg = self.record.get("db")
        target = (
            f"db:{db_cfg['db_schema']}@{db_cfg['url_env']}"
            if db_cfg
            else self.record.get("base_url")
        )
        return {
            "name": self.record["name"],
            "title": self.record.get("title"),
            "tool_count": len(self.tools),
            "kind": "db" if db_cfg else "http",
            "base_url": target,
            "owner": self.record.get("owner", ""),
            "updated_at": self.record.get("updated_at"),
        }


class Registry:
    def __init__(self, cfg: GatewayConfig, host: McpHost, store: Store | None = None) -> None:
        self.cfg = cfg
        self.host = host
        # 持久化后端：默认文件；配了 database_url 则用 pg（由 main 构造时注入）
        self._store: Store = store or FileStore(cfg.data_dir / "services")
        self._services: dict[str, RunningService] = {}
        self._lock = asyncio.Lock()

    async def connect_store(self) -> None:
        """启动时连接持久化后端（pg 建表/迁移；文件后端建目录）。"""
        await self._store.connect()

    async def close_store(self) -> None:
        await self._store.close()

    # ------------------------------------------------------------ 查询

    def get(self, name: str) -> RunningService:
        svc = self._services.get(name)
        if svc is None:
            raise ServiceNotFoundError(name)
        return svc

    def list_services(self) -> list[dict[str, Any]]:
        return [svc.summary() for svc in (self._services[n] for n in sorted(self._services))]

    # ------------------------------------------------------------ 生命周期操作

    async def register(
        self, reg: ServiceRegistration, *, key: str | None = None
    ) -> tuple[RunningService, str, list[str]]:
        """注册新服务；返回 (service, api_key 明文, warnings)。

        key：内部通道（demo 固定演示 key）；None 则随机生成。
        """
        async with self._lock:
            if reg.name in self._services:
                raise ServiceConflictError(f"服务 {reg.name} 已存在；覆盖请使用 PUT /services/{reg.name}")
            if key is not None:
                plain_key = key
                key_hash = hashlib.sha256(key.encode()).hexdigest()
            else:
                plain_key, key_hash = new_api_key()
            svc, warnings = await self._build_service(reg, key_hash=key_hash)
            record = svc.record
            record["created_at"] = _now()
            record["updated_at"] = _now()
            await self._activate(svc)
            return svc, plain_key, warnings

    async def update(self, name: str, reg: ServiceRegistration) -> tuple[RunningService, list[str]]:
        """更新已有服务：保留 api_key 与 created_at；旧 MCP 实例整体替换。"""
        async with self._lock:
            old = self.get(name)  # 404 if missing
            if reg.name != name:
                raise RegistrationError(f"请求体 name({reg.name}) 与路径 name({name}) 不一致")
            svc, warnings = await self._build_service(
                reg, key_hash=old.record["api_key_hash"]
            )
            svc.record["created_at"] = old.record.get("created_at")
            svc.record["updated_at"] = _now()
            await self._activate(svc, old=old)
            return svc, warnings

    async def refresh(self, name: str) -> tuple[RunningService, list[str]]:
        """重拉 spec_url 并热更新：保留 api_key 与 created_at，只刷新 spec 派生的一切。

        仅适用于 spec_url 注册的服务（record 留存有原 URL）；
        spec/spec_text/db 注册源请用 PUT 重新提交。
        """
        async with self._lock:
            old = self.get(name)  # 404 if missing
            spec_url = old.record.get("spec_url")
            if not spec_url:
                raise RegistrationError(
                    f"服务 {name} 不是 spec_url 注册（record 无 spec_url），请用 PUT 重新提交"
                )
            rec = old.record
            reg = ServiceRegistration(
                name=rec["name"],
                title=rec.get("title"),
                spec_url=spec_url,
                base_url=rec.get("base_url"),
                auth=AuthConfig(**rec["auth"]) if rec.get("auth") else None,
                filters=Filters(**rec.get("filters", {})),
                max_tools=rec.get("max_tools"),
                timeout_s=rec.get("timeout_s", 30.0),
                owner=rec.get("owner", ""),
                response_template=rec.get("response_template"),
                tool_response_templates=rec.get("tool_response_templates", {}),
                allowed_tools=rec.get("allowed_tools") or [],
            )
            svc, warnings = await self._build_service(reg, key_hash=rec["api_key_hash"])
            svc.record["created_at"] = rec.get("created_at")
            svc.record["updated_at"] = _now()
            await self._activate(svc, old=old)
            return svc, warnings

    async def remove(self, name: str) -> None:
        async with self._lock:
            self.get(name)
            del self._services[name]
            await self.host.uninstall(name)
            await self._store.delete(name)

    async def health(self, name: str) -> dict[str, Any]:
        """目标 API 健康探测：HEAD（405 时回退 GET）+ 凭证环境变量自检。

        base_url 注册时已过 allowlist，探测不引入新攻击面；凭证只查存在性，不回传值。
        """
        svc = self.get(name)
        # DB 源（M5）：SELECT 1 探测 + DSN 脱敏展示，凭证只查存在性
        db_exec = svc.db_executor
        if db_exec is not None:
            db_cfg = svc.record["db"]
            base: dict[str, Any] = {
                "service": name,
                "target": dsn_safe_display(db_exec._dsn),
                "kind": "db",
                "auth_env_ok": bool(os.environ.get(db_cfg["url_env"])),
                "checked_at": _now(),
            }
            if not base["auth_env_ok"]:
                base["auth_error"] = f"环境变量 {db_cfg['url_env']} 未配置"
            start = time.monotonic()
            ok, error = await db_exec.ping()
            base["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
            base["reachable"] = ok
            base["ok"] = ok and base["auth_env_ok"]
            if error:
                base["error"] = error
            return base

        assert svc.invoker is not None
        cfg = svc.invoker.cfg
        headers: dict[str, str] = {}
        params: dict[str, Any] = {}
        auth_error = apply_auth(cfg.auth, headers, params)
        base: dict[str, Any] = {
            "service": name,
            "target": cfg.base_url,
            "auth_env_ok": auth_error is None,
            "checked_at": _now(),
        }
        if auth_error:
            base["auth_error"] = auth_error
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5.0, follow_redirects=False) as c:
                resp = await c.head(cfg.base_url, headers=headers, params=params or None)
                if resp.status_code in (405, 501):  # 不少 API 不支持 HEAD
                    resp = await c.get(cfg.base_url, headers=headers, params=params or None)
            base.update(
                reachable=True,
                http_status=resp.status_code,
                ok=resp.status_code < 400,
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
        except httpx.HTTPError as exc:
            base.update(
                reachable=False,
                ok=False,
                latency_ms=round((time.monotonic() - start) * 1000, 1),
                error=f"{exc.__class__.__name__}: {exc}"[:200],
            )
        return base

    async def rotate_key(self, name: str) -> str:
        async with self._lock:
            svc = self.get(name)
            plain, key_hash = new_api_key()
            svc.record["api_key_hash"] = key_hash
            svc.record["updated_at"] = _now()
            await self._store.save(svc.record)
            # key 校验在 McpHost 层，替换哈希即生效，无需重启 MCP 实例
            self.host.rekey(name, key_hash)
            return plain

    async def shutdown(self) -> None:
        async with self._lock:
            for name in list(self._services):
                del self._services[name]
            await self.host.shutdown()

    # ------------------------------------------------------------ 流水线

    async def preview(
        self, reg: ServiceRegistration, sample_response: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """预览注册结果：完整走一遍流水线但不落盘、不安装、不生成 key。"""
        svc, warnings = await self._build_service(reg, key_hash="")
        out: dict[str, Any] = {
            "name": reg.name,
            "title": svc.record["title"],
            "base_url": svc.record["base_url"],
            "tool_count": len(svc.tools),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in svc.tools
            ],
            "warnings": warnings,
        }
        if reg.allowed_tools:
            out["exposed_tool_count"] = sum(
                1 for t in svc.tools if t.name in set(reg.allowed_tools)
            )
        if sample_response is not None:
            out["template_preview"] = {
                t.name: render_response(
                    t.response_template,
                    response=sample_response,
                    status=200,
                    tool=t.name,
                )
                for t in svc.tools
                if t.response_template
            }
        return out

    async def _build_service(self, reg: ServiceRegistration, *, key_hash: str) -> tuple[RunningService, list[str]]:
        """注册流水线：spec 归一化 → 提取/生成 → 模板挂载 → 组装运行时组件。"""
        warnings: list[str] = []

        # 0. DB 源（M5）：独立小流水线（内省 → 三件套工具 → 只读执行器）
        if reg.db is not None:
            return await self._build_db_service(reg, key_hash=key_hash, warnings=warnings)

        # 1. spec 来源：spec_text 原文解析（JSON→YAML 兑底）；spec_url 逐跳过 allowlist，防重定向绕过
        if reg.spec is None and reg.spec_text is None and not reg.spec_url:
            raise RegistrationError("spec / spec_text / spec_url / db 至少填一个（db 为数据库注册源）")
        if reg.spec is not None:
            source: dict[str, Any] | str = reg.spec
        elif reg.spec_text is not None:
            source = parse_spec_text(reg.spec_text)
        else:
            source = reg.spec_url  # type: ignore[assignment]
        raw_spec, _ = load_spec(source, allowlist_check=self.cfg.target_host_allowed)

        # 2. 归一化（Swagger 2.0 → OAS3）+ 校验 + $ref 解析
        spec_doc, norm_warnings = normalize_spec(raw_spec)
        warnings.extend(norm_warnings)
        from .loader import resolve_refs, validate_spec

        warnings.extend(validate_spec(spec_doc))
        try:
            spec = resolve_refs(spec_doc)
        except SpecError:
            raise

        # 3. base_url：显式配置 > spec.servers[0]
        base_url = reg.base_url
        if not base_url:
            servers = spec.get("servers") or []
            if servers and isinstance(servers[0], dict):
                base_url = servers[0].get("url")
                if base_url and "{" in base_url:
                    warnings.append(f"spec.servers 含变量占位符，已忽略: {base_url}")
                    base_url = None
        if not base_url:
            raise RegistrationError(
                "无法确定目标 API 地址：注册未传 base_url，spec 中也没有可用的 servers"
            )
        if not base_url.startswith(("http://", "https://")):
            raise RegistrationError(f"base_url 必须以 http(s):// 开头: {base_url}")
        try:
            await self.cfg.ensure_target_allowed(base_url)
        except TargetNotAllowed as exc:
            raise RegistrationError(str(exc)) from exc

        # oauth2 token_url 由网关代发请求（与 base_url 同攻击面），注册期同样过 allowlist
        if reg.auth is not None and reg.auth.type == "oauth2":
            try:
                await self.cfg.ensure_target_allowed(reg.auth.token_url)
            except TargetNotAllowed as exc:
                raise RegistrationError(str(exc)) from exc

        # 4. 接口提取 + 工具生成
        endpoints, extract_warnings = extract_endpoints(spec, reg.filters)
        warnings.extend(extract_warnings)
        max_tools = reg.max_tools or self.cfg.default_max_tools
        tools, gen_warnings = generate_tools(endpoints, max_tools=max_tools)
        warnings.extend(gen_warnings)

        # 4.5 响应模板：注册期校验语法 + 挂载到 ToolDef（工具级覆盖 > 服务级默认）
        if reg.response_template:
            try:
                validate_template(reg.response_template)
            except TemplateError as exc:
                raise RegistrationError(f"response_template 不合法: {exc}") from exc
        tool_names = {t.name for t in tools}
        for tname, tpl in (reg.tool_response_templates or {}).items():
            try:
                validate_template(tpl)
            except TemplateError as exc:
                raise RegistrationError(f"工具 {tname} 的 response_template 不合法: {exc}") from exc
            if tname not in tool_names:
                warnings.append(f"tool_response_templates 中的 {tname} 未命中生成的工具，已忽略")
        for t in tools:
            t.response_template = reg.tool_response_templates.get(t.name) or reg.response_template

        # 5. 运行时组件（allowed_tools 未命中生成工具时仅告警，不阻断注册）
        if reg.allowed_tools:
            unknown = set(reg.allowed_tools) - tool_names
            if unknown:
                warnings.append(f"allowed_tools 中的 {sorted(unknown)} 未命中生成的工具，已忽略")
        http_cfg = ServiceHttpConfig(base_url=base_url, auth=reg.auth, timeout_s=reg.timeout_s)
        invoker = Invoker(http_cfg)
        app = McpServiceApp(
            reg.name, reg.title, tools, invoker, allowed_tools=reg.allowed_tools or None
        )

        record: dict[str, Any] = {
            "name": reg.name,
            "title": reg.title,
            "spec": raw_spec,  # 落盘存原文（含 Swagger 2.0 原稿），恢复时重走流水线
            # spec_url 注册源额外留存原 URL：供 POST /services/{name}/refresh 重拉；
            # 启动恢复仍用上面的 spec 快照（不依赖外网可达性）
            "spec_url": reg.spec_url,
            "base_url": base_url,
            "auth": reg.auth.model_dump() if reg.auth else None,
            "filters": reg.filters.model_dump(),
            "max_tools": reg.max_tools,
            "timeout_s": reg.timeout_s,
            "owner": reg.owner,
            "response_template": reg.response_template,
            "tool_response_templates": reg.tool_response_templates,
            "allowed_tools": reg.allowed_tools,
            "api_key_hash": key_hash,
            "created_at": None,
            "updated_at": None,
        }
        return RunningService(record=record, tools=tools, invoker=invoker, app=app), warnings

    async def _build_db_service(
        self, reg: ServiceRegistration, *, key_hash: str, warnings: list[str]
    ) -> tuple[RunningService, list[str]]:
        """DB 源流水线：DSN 环境变量 → allowlist → 内省 → 三件套工具 → 只读执行器。

        与 REST 流水线共享：key 生成/校验、allowed_tools、落盘恢复、挂载、观测。
        """
        from .db import (
            DbExecutor,
            build_db_tools,
            check_driver,
            close_db_pool,
            create_db_pool,
            dialect_of,
            introspect,
        )

        db = reg.db
        assert db is not None
        dsn = os.environ.get(db.url_env)
        if not dsn:
            raise RegistrationError(
                f"环境变量 {db.url_env} 未配置（DSN 含密码只经环境变量引用，不落盘）"
            )
        try:
            dialect = dialect_of(dsn)
            check_driver(dsn)
        except ValueError as exc:
            raise RegistrationError(str(exc)) from exc
        # MySQL 语义：schema 参数即 database；缺省取 DSN 中的库名
        schema = db.db_schema
        if dialect == "mysql" and schema == "public":
            # MySQL 无 schema 概念：未显式配置时取 DSN 库名（回写后落盘/工具描述一致）
            schema = urlparse(dsn).path.lstrip("/") or "public"
            db.db_schema = schema
        # DSN host 同走目标 allowlist（DB 是 TCP，但 host 语义一致：私网放行/可显式收紧）
        try:
            await self.cfg.ensure_target_allowed(dsn)
        except TargetNotAllowed as exc:
            raise RegistrationError(str(exc)) from exc

        try:
            pool = await create_db_pool(dsn, db.timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise RegistrationError(f"数据库连接失败: {str(exc)[:200]}") from exc
        try:
            catalog = await introspect(pool, dialect, schema, db.include_tables, db.exclude_tables)
        except Exception as exc:  # noqa: BLE001
            raise RegistrationError(f"数据库内省失败: {str(exc)[:200]}") from exc
        finally:
            await close_db_pool(pool)
        if not catalog:
            raise RegistrationError(
                f"schema {schema} 内省无表（检查 schema / include_tables / exclude_tables）"
            )

        tools = build_db_tools(catalog, db)
        tool_names = {t.name for t in tools}
        if reg.allowed_tools:
            unknown = set(reg.allowed_tools) - tool_names
            if unknown:
                warnings.append(f"allowed_tools 中的 {sorted(unknown)} 未命中生成的工具，已忽略")

        executor = DbExecutor(db, dsn)
        executor.bind_catalog(catalog)
        app = McpServiceApp(
            reg.name,
            reg.title or f"{reg.name} 数据库",
            tools,
            allowed_tools=reg.allowed_tools or None,
            db_executor=executor,
        )
        record: dict[str, Any] = {
            "name": reg.name,
            "title": reg.title or f"{reg.name} 数据库",
            "db": db.model_dump(),
            "owner": reg.owner,
            "allowed_tools": reg.allowed_tools,
            "api_key_hash": key_hash,
            "created_at": None,
            "updated_at": None,
        }
        return (
            RunningService(record=record, tools=tools, app=app, db_executor=executor),
            warnings,
        )

    async def _activate(
        self, svc: RunningService, *, old: RunningService | None = None
    ) -> None:
        """挂载 + 登记 + 落盘；落盘失败时回滚内存态，保证 store 与运行态一致。

        update 场景旧实例已被 install 内部 stop（invoker 连接已关，不可复用），
        落盘失败时不回滚旧实例：新版本继续在内存运行，重启后回退旧配置；
        错误信息会提示重试 PUT。
        """
        name = svc.record["name"]
        await self.host.install(name, svc.app, svc.record["api_key_hash"])
        self._services[name] = svc
        try:
            await self._store.save(svc.record)
        except Exception as exc:  # noqa: BLE001
            if old is None:
                # 新注册：回滚，对外表现为注册失败（可修复后重试）
                del self._services[name]
                await self.host.uninstall(name)
                raise RegistrationError(
                    f"服务 {name} 注册成功但持久化失败，已回滚: {str(exc)[:150]}"
                ) from exc
            logger.warning(
                "服务 %s 更新已在内存生效但持久化失败（重启后将回退旧配置，重试 PUT 可恢复）: %s",
                name,
                exc,
            )
            raise RegistrationError(
                f"服务 {name} 更新已生效但持久化失败，重启后将回退旧配置；"
                f"请检查存储后端后重试 PUT: {str(exc)[:120]}"
            ) from exc

    # ------------------------------------------------------------ 启动恢复

    async def load_from_store(self) -> list[str]:
        """启动恢复：从持久化后端全量重建，单条坏记录跳过不拖垮网关。"""
        restored: list[str] = []
        for record in await self._store.load_all():
            name = record.get("name") or "?"
            try:
                reg = ServiceRegistration(
                    name=record["name"],
                    title=record.get("title"),
                    spec=record.get("spec"),
                    base_url=record.get("base_url"),
                    auth=AuthConfig(**record["auth"]) if record.get("auth") else None,
                    filters=Filters(**record.get("filters", {})),
                    max_tools=record.get("max_tools"),
                    timeout_s=record.get("timeout_s", 30.0),
                    owner=record.get("owner", ""),
                    response_template=record.get("response_template"),
                    tool_response_templates=record.get("tool_response_templates", {}),
                    allowed_tools=record.get("allowed_tools") or [],
                    db=record.get("db"),
                )
                svc, _ = await self._build_service(reg, key_hash=record["api_key_hash"])
                # 恢复用 spec 快照重建，但 spec_url 元数据要回挂（供 refresh 重拉）
                svc.record["spec_url"] = record.get("spec_url")
                svc.record["created_at"] = record.get("created_at")
                svc.record["updated_at"] = record.get("updated_at")
                await self._activate(svc)
                restored.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("恢复服务 %s 失败，已跳过: %s", name, exc)
        if restored:
            logger.info("启动恢复 %d 个服务: %s", len(restored), ", ".join(restored))
        return restored
