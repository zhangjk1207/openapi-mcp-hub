"""网关级配置：全部来自环境变量。"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ENV_PREFIX = "OPENAPI_MCP_HUB"

# 默认单服务工具数软上限（工具过多会显著降低 LLM 选择准确率）
DEFAULT_MAX_TOOLS = 100
# openapi 文本体积上限（字节）
MAX_SPEC_BYTES = 10 * 1024 * 1024
# $ref 解析深度上限
MAX_REF_DEPTH = 64


@dataclass
class GatewayConfig:
    admin_token: str | None
    data_dir: Path
    host_allowlist: list[str] = field(default_factory=list)
    host: str = "0.0.0.0"
    port: int = 20033
    default_max_tools: int = DEFAULT_MAX_TOOLS
    # PostgreSQL 持久化（可选）：不配则用 data/services/*.json 文件后端
    database_url: str | None = None
    # /docs 与 /openapi.json：默认关闭。管理面 schema 没必要对外暴露，
    # 控制台已覆盖全部操作；需要时用 OPENAPI_MCP_HUB_DOCS=1 打开。
    docs_enabled: bool = False

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        admin_token = os.environ.get(f"{ENV_PREFIX}_ADMIN_TOKEN") or None
        data_dir = Path(os.environ.get(f"{ENV_PREFIX}_DATA_DIR", "./data"))
        allowlist_raw = os.environ.get(f"{ENV_PREFIX}_HOST_ALLOWLIST", "").strip()
        allowlist = [x.strip() for x in allowlist_raw.split(",") if x.strip()]
        host = os.environ.get(f"{ENV_PREFIX}_HOST", "0.0.0.0")
        port = int(os.environ.get(f"{ENV_PREFIX}_PORT", "20033"))
        database_url = os.environ.get(f"{ENV_PREFIX}_DATABASE_URL") or None
        docs_enabled = os.environ.get(f"{ENV_PREFIX}_DOCS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            admin_token=admin_token,
            data_dir=data_dir,
            host_allowlist=allowlist,
            host=host,
            port=port,
            database_url=database_url,
            docs_enabled=docs_enabled,
        )

    def target_host_allowed(self, url: str) -> bool:
        """字符串层校验目标主机是否在 allowlist 内（防 SSRF，同步、无 DNS）。

        规则：
        - allowlist 为空 => 不限制（内网默认）；
        - 条目为 IP/CIDR => 目标 host 是 IP 且命中网段（IPv4-mapped IPv6
          如 ``::ffff:10.0.0.1`` 会先规范化为 IPv4 再比对，防绕过/防误杀）；
        - 其他 => 与目标 host 精确相等，或以 ``.entry`` 结尾（子域名）。
        """
        if not self.host_allowlist:
            return True
        host = _normalize_host((urlparse(url).hostname or "").lower())
        if not host:
            return False
        for entry in self.host_allowlist:
            entry = entry.strip().lower()
            if not entry:
                continue
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                if host == entry or host.endswith("." + entry):
                    return True
                continue
            try:
                if ipaddress.ip_address(host) in net:
                    return True
            except ValueError:
                continue
        return False

    def _host_matches_by_name(self, host: str) -> bool:
        """host 是否被 allowlist 中的名字条目（精确/子域名）直接信任。"""
        for entry in self.host_allowlist:
            entry = entry.strip().lower()
            if not entry:
                continue
            try:
                ipaddress.ip_network(entry, strict=False)
                continue  # IP/CIDR 条目不算名字信任
            except ValueError:
                pass
            if host == entry or host.endswith("." + entry):
                return True
        return False

    def _host_in_allowed_nets(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(_normalize_host(ip))
        except ValueError:
            return False
        for entry in self.host_allowlist:
            try:
                net = ipaddress.ip_network(entry.strip().lower(), strict=False)
            except ValueError:
                continue
            if addr in net:
                return True
        return False

    async def ensure_target_allowed(self, url: str) -> None:
        """注册期完整校验目标 URL（字符串层 + DNS 解析核验，防 DNS rebinding）。

        判定顺序：
        1. allowlist 为空 => 放行（内网默认）；
        2. scheme 仅 http/https；
        3. host 被名字条目信任（精确/子域名）=> 放行（显式信任，不做解析）；
        4. host 是 IP 字面量 => 必须命中 CIDR 条目；
        5. 域名 => 解析后所有 IP 必须命中 CIDR 条目（防注册时填外网域名、
           解析后再指向内网，或反之绕过 allowlist）。

        不合法抛 TargetNotAllowed（registry 转 422）。
        """
        if not self.host_allowlist:
            return
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise TargetNotAllowed(f"仅支持 http(s) 目标: {url}")
        host = _normalize_host((parsed.hostname or "").lower())
        if not host:
            raise TargetNotAllowed(f"URL 缺少主机名: {url}")
        if self._host_matches_by_name(host):
            return
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if self._host_in_allowed_nets(host):
                return
            raise TargetNotAllowed(
                f"目标 IP 不在 allowlist 网段内: {host}（检查 OPENAPI_MCP_HUB_HOST_ALLOWLIST）"
            )
        # 域名：先确认 allowlist 有网段条目可核验，再解析比对
        if not self._has_net_entries():
            raise TargetNotAllowed(
                f"目标主机 {host} 不在 allowlist 内（allowlist 只有名字条目，"
                "无法核验未列名域名）"
            )
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError as exc:
            raise TargetNotAllowed(f"目标域名解析失败: {host}（{exc}）") from exc
        ips = {info[4][0] for info in infos}
        bad = [ip for ip in ips if not self._host_in_allowed_nets(ip)]
        if bad:
            raise TargetNotAllowed(
                f"目标域名 {host} 解析出不在 allowlist 网段内的 IP: {sorted(bad)}"
            )

    def _has_net_entries(self) -> bool:
        for entry in self.host_allowlist:
            try:
                ipaddress.ip_network(entry.strip().lower(), strict=False)
                return True
            except ValueError:
                continue
        return False


class TargetNotAllowed(ValueError):
    """目标主机未通过 allowlist 校验（SSRF 防护）。"""


def _normalize_host(host: str) -> str:
    """IPv4-mapped IPv6（::ffff:10.0.0.1）规范化为 IPv4 字符串，其余原样。"""
    if ":" in host:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return host
        if getattr(addr, "ipv4_mapped", None) is not None:
            return str(addr.ipv4_mapped)
    return host
