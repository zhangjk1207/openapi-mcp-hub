"""CLI 参数与生效配置测试（main.py / config.py docs 开关）。"""

from __future__ import annotations

import pytest

from openapi_mcp_hub.config import GatewayConfig
from openapi_mcp_hub.main import _apply_cli_overrides, _build_parser, _describe_config


def _parse(argv: list[str]):
    return _build_parser().parse_args(argv)


def test_no_args_keeps_env_defaults(monkeypatch):
    """不传任何参数时行为与旧版完全一致（纯环境变量驱动）。"""
    monkeypatch.setenv("OPENAPI_MCP_HUB_PORT", "21000")
    monkeypatch.setenv("OPENAPI_MCP_HUB_HOST", "127.0.0.1")
    cfg = _apply_cli_overrides(GatewayConfig.from_env(), _parse([]))
    assert cfg.port == 21000
    assert cfg.host == "127.0.0.1"


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENAPI_MCP_HUB_PORT", "21000")
    cfg = _apply_cli_overrides(
        GatewayConfig.from_env(), _parse(["--port", "9999", "--host", "0.0.0.0"])
    )
    assert cfg.port == 9999
    assert cfg.host == "0.0.0.0"


def test_cli_data_dir_and_database_url(tmp_path):
    cfg = _apply_cli_overrides(
        GatewayConfig.from_env(),
        _parse(["--data-dir", str(tmp_path), "--database-url", "postgresql://u:p@h/db"]),
    )
    assert cfg.data_dir == tmp_path
    assert cfg.database_url == "postgresql://u:p@h/db"


def test_cli_host_allowlist_splits_and_strips():
    cfg = _apply_cli_overrides(
        GatewayConfig.from_env(), _parse(["--host-allowlist", " 10.0.0.0/8 , api.internal ,"])
    )
    assert cfg.host_allowlist == ["10.0.0.0/8", "api.internal"]


def test_docs_off_by_default_and_can_be_enabled(monkeypatch):
    monkeypatch.delenv("OPENAPI_MCP_HUB_DOCS", raising=False)
    assert GatewayConfig.from_env().docs_enabled is False
    assert _apply_cli_overrides(GatewayConfig.from_env(), _parse(["--docs"])).docs_enabled is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_docs_env_truthy(monkeypatch, value):
    monkeypatch.setenv("OPENAPI_MCP_HUB_DOCS", value)
    assert GatewayConfig.from_env().docs_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "", "off", "no"])
def test_docs_env_falsy(monkeypatch, value):
    monkeypatch.setenv("OPENAPI_MCP_HUB_DOCS", value)
    assert GatewayConfig.from_env().docs_enabled is False


def test_print_config_never_leaks_admin_token():
    cfg = GatewayConfig(admin_token="super-secret-token", data_dir=__import__("pathlib").Path("/tmp"))
    out = _describe_config(cfg)
    assert "super-secret-token" not in out
    assert "已配置" in out


def test_print_config_reports_file_vs_pg():
    from pathlib import Path

    file_cfg = GatewayConfig(admin_token=None, data_dir=Path("/tmp"))
    assert "file" in _describe_config(file_cfg)
    pg_cfg = GatewayConfig(
        admin_token=None, data_dir=Path("/tmp"), database_url="postgresql://u:p@h/db"
    )
    assert "PostgreSQL" in _describe_config(pg_cfg)


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        _parse(["--help"])
    assert exc.value.code == 0
    assert "openapi-mcp-hub" in capsys.readouterr().out
