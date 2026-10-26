"""版本号一致性：`__version__` 必须与 pyproject.toml 的 version 相同。

两个来源一旦漂移，`/healthz`、控制台和 PyPI 页面会各说各话 —— 这类漂移没人在 CI 里盯着
就一定会发生（0.4.0 之后已经出现过一次）。
"""

from __future__ import annotations

import re
from pathlib import Path

import openapi_mcp_hub


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version = "(.+?)"', text, re.M)
    assert match is not None, "pyproject.toml 里找不到 version"
    assert openapi_mcp_hub.__version__ == match.group(1)
