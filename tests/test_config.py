"""allowlist / SSRF 校验单测（config.py）。

覆盖：IPv4-mapped IPv6 规范化、名字/子域名信任、DNS 解析核验（防 rebinding）。
"""

from __future__ import annotations

import pytest

from api_to_mcp.config import GatewayConfig, TargetNotAllowed, _normalize_host


def _cfg(allowlist: list[str]) -> GatewayConfig:
    return GatewayConfig(admin_token="t", data_dir=None, host_allowlist=allowlist)  # type: ignore[arg-type]


class TestNormalizeHost:
    def test_mapped_v6_to_v4(self):
        assert _normalize_host("::ffff:10.1.2.3") == "10.1.2.3"

    def test_plain_v6_untouched(self):
        assert _normalize_host("fd00::1") == "fd00::1"

    def test_hostname_untouched(self):
        assert _normalize_host("api.example.com") == "api.example.com"


class TestTargetHostAllowed:
    def test_empty_allowlist_allows_all(self):
        assert _cfg([]).target_host_allowed("http://8.8.8.8/x")

    def test_cidr_hit(self):
        assert _cfg(["10.0.0.0/8"]).target_host_allowed("http://10.1.2.3:8080/x")

    def test_cidr_miss(self):
        assert not _cfg(["10.0.0.0/8"]).target_host_allowed("http://8.8.8.8/x")

    def test_mapped_v6_normalized_for_cidr(self):
        # ::ffff:10.1.2.3 规范化为 10.1.2.3 后命中网段（防绕过/防误杀）
        assert _cfg(["10.0.0.0/8"]).target_host_allowed("http://[::ffff:10.1.2.3]/x")
        assert not _cfg(["10.0.0.0/8"]).target_host_allowed("http://[::ffff:8.8.8.8]/x")

    def test_exact_host(self):
        assert _cfg(["api.example.com"]).target_host_allowed("https://api.example.com/v1")

    def test_subdomain(self):
        assert _cfg(["example.com"]).target_host_allowed("https://a.b.example.com/v1")
        assert not _cfg(["example.com"]).target_host_allowed("https://notexample.com/v1")

    def test_evil_suffix_not_matched(self):
        # 后缀相同但不是子域名：必须拒绝
        assert not _cfg(["good.com"]).target_host_allowed("http://evilgood.com/x")


class TestEnsureTargetAllowed:
    @pytest.mark.asyncio
    async def test_name_trusted_skips_dns(self):
        cfg = _cfg(["api.example.com"])
        await cfg.ensure_target_allowed("https://api.example.com/v1")  # 不解析 DNS 也通过

    @pytest.mark.asyncio
    async def test_dns_all_in_net(self, monkeypatch):
        cfg = _cfg(["10.0.0.0/8"])
        monkeypatch.setattr(
            "api_to_mcp.config.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("10.1.1.1", 0))],
        )
        await cfg.ensure_target_allowed("http://internal.corp/x")

    @pytest.mark.asyncio
    async def test_dns_rebinding_rejected(self, monkeypatch):
        # 域名解析出网段外 IP => 拒绝（防注册合法域名后 rebinding 到内网）
        cfg = _cfg(["10.0.0.0/8"])
        monkeypatch.setattr(
            "api_to_mcp.config.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("192.168.1.1", 0))],
        )
        with pytest.raises(TargetNotAllowed, match="不在 allowlist 网段内"):
            await cfg.ensure_target_allowed("http://internal.corp/x")

    @pytest.mark.asyncio
    async def test_dns_mixed_ips_rejected(self, monkeypatch):
        # 任一解析 IP 不在网段 => 拒绝
        cfg = _cfg(["10.0.0.0/8"])
        monkeypatch.setattr(
            "api_to_mcp.config.socket.getaddrinfo",
            lambda host, port: [
                (None, None, None, None, ("10.1.1.1", 0)),
                (None, None, None, None, ("192.168.1.1", 0)),
            ],
        )
        with pytest.raises(TargetNotAllowed):
            await cfg.ensure_target_allowed("http://internal.corp/x")

    @pytest.mark.asyncio
    async def test_dns_failure_rejected(self, monkeypatch):
        import socket

        def boom(host, port):
            raise socket.gaierror("dns boom")

        cfg = _cfg(["10.0.0.0/8"])
        monkeypatch.setattr("api_to_mcp.config.socket.getaddrinfo", boom)
        with pytest.raises(TargetNotAllowed, match="解析失败"):
            await cfg.ensure_target_allowed("http://internal.corp/x")

    @pytest.mark.asyncio
    async def test_ip_literal_checked_without_dns(self, monkeypatch):
        cfg = _cfg(["10.0.0.0/8"])

        def boom(host, port):  # 若误走 DNS 会炸
            raise AssertionError("IP 字面量不应触发 DNS 解析")

        monkeypatch.setattr("api_to_mcp.config.socket.getaddrinfo", boom)
        await cfg.ensure_target_allowed("http://10.2.3.4/x")
        with pytest.raises(TargetNotAllowed):
            await cfg.ensure_target_allowed("http://8.8.8.8/x")

    @pytest.mark.asyncio
    async def test_non_http_scheme_rejected(self):
        cfg = _cfg(["10.0.0.0/8"])
        with pytest.raises(TargetNotAllowed, match="http"):
            await cfg.ensure_target_allowed("ftp://10.0.0.1/x")
