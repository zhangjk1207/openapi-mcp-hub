# Contributing to openapi-mcp-hub

Thanks for considering a contribution.

## Development setup

```bash
git clone https://github.com/zhangjk1207/openapi-mcp-hub
cd openapi-mcp-hub
python3 -m pip install -e ".[dev]"
```

Python **3.10+**; CI covers 3.10 / 3.11 / 3.12.

## Before you open a PR

1. **Lint**: `ruff check src tests` — must pass (CI runs it on every push and PR).
2. **Tests**: `pytest` — all tests must pass. The suite uses `pytest-asyncio` for async paths and `respx` for HTTP mocking; CI additionally runs the Postgres-backed tests against a real `postgres:16` service.
3. **Public surface is contractual**: the gateway API (`/services`, `/mcp/{service}`), environment variables and config file formats. Breaking changes need a strong reason and a migration note in the PR description.
4. New registration-source or MCP behaviors should come with a test under `tests/`.

## What's welcome

- Bug fixes with a reproducing test
- New registration sources (e.g., additional read-only DB dialects) behind the existing admin plane
- Docs improvements — `README.md` and `README.zh-CN.md` should stay in sync
- Performance and security hardening

## Reporting bugs

Please use the issue templates and include: gateway version (or commit), Python version, run mode (`docker compose` / `openapi-mcp-hub` entry point / from source), the registration payload (redact all tokens and keys), and logs.

## Commits & PRs

- PRs target `main`.
- Any commit style is fine; keep the PR description informative — what, why, and how it was tested.
- CI must be green before review.

## License

MIT — by contributing you agree that your contributions are licensed under it.
