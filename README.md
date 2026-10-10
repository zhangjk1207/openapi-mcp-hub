# openapi-mcp-hub

**Turn any OpenAPI spec — or a live PostgreSQL / MySQL database — into production-ready MCP endpoints. One process. One port. Zero code.**

[![CI](https://github.com/zhangjk1207/openapi-mcp-hub/actions/workflows/ci.yml/badge.svg)](../../actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-6e7bf4.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](pyproject.toml)
[![中文文档](https://img.shields.io/badge/README-中文-red.svg)](README.zh-CN.md)

POST an `openapi.json` to the gateway and you instantly get an MCP endpoint any agent can call:

```
POST /services        →   http://your-gateway:20033/mcp/{service-name}
```

No wrapper code. No per-spec deployment. No port sprawl. Register as many services as you like — REST APIs or read-only databases — all behind a single port, hot-loaded the moment they register.

> 🇨🇳 [中文文档看这里](README.zh-CN.md) · 📖 Full usage guide: [USAGE.md](USAGE.md) (中文)

---

## Why

- **OpenAPI/Swagger** is the de-facto standard for describing REST APIs — most internal services already have a spec.
- **MCP** (Model Context Protocol) is how agents consume tools — Claude Code and friends speak it natively over Streamable HTTP.
- Hand-writing MCP wrappers for every endpoint is repetitive and drifts out of sync. Running *one MCP server per spec* means port sprawl and linear ops cost.

**openapi-mcp-hub is a gateway**: one resident process, one port, path-level multi-tenancy (`/mcp/{service}`). Registration takes effect immediately — no restarts.

```text
             ┌──────────── admin plane (admin token) ─────────────┐
             │  POST /services            register (REST or DB)   │
             │  GET  /services            list / inspect          │
             │  POST /services/preview    dry-run registration    │
             └────────────────────┬───────────────────────────────┘
                                  ▼
   Agents ── MCP (streamable HTTP / SSE) ──▶  openapi-mcp-hub gateway  ── HTTP ──▶  your APIs
   Claude Code, Cursor, custom clients        single port :20033   ── SQL ──▶  PostgreSQL / MySQL 8+
                                             per-service api_key
```

## Quickstart

```bash
git clone https://github.com/zhangjk1207/openapi-mcp-hub && cd openapi-mcp-hub
docker compose up          # gateway + demo mock API, auto-registers a petstore service
```

Open **http://localhost:20033** — the built-in admin console loads (paste the admin token printed on first boot, saved in `deploy/admin_token`). You should see the demo service listed.

Register your own API:

```bash
ADMIN_TOKEN=$(cat deploy/admin_token)

# spec_text receives the raw file content (JSON or YAML)
jq -n --rawfile s your-openapi.json '{name:"my-api", base_url:"https://api.example.com", spec_text:$s}' | \
  curl -X POST http://localhost:20033/services \
    -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" -d @-
```

Connect it to Claude Code (the response includes a ready-to-run command):

```bash
claude mcp add --transport http my-api http://localhost:20033/mcp/my-api \
  --header 'X-Api-Key: psk-xxxx'
```

That's it — your API's operations are now agent-callable tools.

## Register a database instead of an API

No REST spec needed. Point the gateway at a database (via an **environment variable name** — the DSN itself never touches disk):

```bash
export WORKFLOW_DB_URL='postgresql://ro_user:pass@db.internal.example:5432/workflow'
curl -X POST http://localhost:20033/services \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"workflow-db","db":{"url_env":"WORKFLOW_DB_URL"}}'
```

You get three tools the agent can use blind: `list_tables`, `describe_table` (schema + comments embedded), and `query` (SELECT-only).

### Read-only is enforced in six layers

| Layer | What it does |
|---|---|
| Lexical whitelist | First token must be `SELECT`/`WITH`/`EXPLAIN`; comments blocked; string literals respected |
| `READ ONLY` transactions | Server-enforced — writes fail even if they slip past the lexer (PG / MySQL 8+) |
| Outer `LIMIT` wrap | `SELECT * FROM (your SQL) sub LIMIT max_rows+1` — result size capped regardless |
| Statement timeout | `statement_timeout` (PG) / `max_execution_time` (MySQL) per pool |
| Isolated pool | Separate connection pool per service, `minsize=1, maxsize=5` |
| Table filtering | `include_tables` / `exclude_tables` at introspection time |

Credentials are referenced **by env var name only** — nothing sensitive is persisted or logged. SQL digests land in the in-memory call log for auditing.

## Features

| | |
|---|---|
| **Sources** | OpenAPI 3.0/3.1, Swagger 2.0 (auto-normalized), spec URL with SSRF allowlist, JSON/YAML upload, PostgreSQL, MySQL 8+ |
| **Transport** | Streamable HTTP + legacy SSE (`/mcp/{name}/sse`) on one port |
| **Multi-tenant security** | Per-service `api_key` (shown once, stored hashed, rotatable), admin token for the management plane |
| **Hot registration** | Register / update / delete without restarts; failures roll back atomically |
| **Filtering** | Path include/exclude globs, tool whitelist, `max_tools` cap, response templates |
| **Persistence** | JSON file or PostgreSQL backend, automatic one-way migration |
| **Console** | Single-file admin UI, zero build chain, zero external assets — works in air-gapped intranets |
| **Observability** | Health probes, in-memory ring call log (last 20/service), structured error codes (`-32000`/-`32602`/-`32002`) |

## Comparison

| | openapi-mcp-hub | one-MCP-server-per-spec | general API gateways + MCP plugin |
|---|---|---|---|
| New service | POST a spec, ~1s | Write & deploy a wrapper | Plugin config + reload |
| Ports | **1** | N | 1 (heavyweight) |
| Database → MCP | **Built-in, read-only enforced** | Separate project | Rarely |
| Air-gapped console | **Yes, single HTML file** | — | Usually needs internet |
| Runtime deps | FastAPI + mcp SDK (~10 packages) | varies | Full gateway stack |

## Console screenshots

Register (with file upload / drag & drop), inspect tools & call logs, rotate keys — all from one static page:

<p align="center"><em>screenshots coming with the first tagged release</em></p>

## Roadmap

- [x] M5-1 — Database → MCP (PostgreSQL + MySQL 8, six-layer read-only)
- [ ] M5-2 — per-table tool mode, sensitive-column masking
- [ ] M5-3 — write operations (opt-in, statement allowlist)
- [ ] Metrics endpoint (Prometheus) & key expiry
- [ ] Per-tenant quotas / rate limiting

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q          # 164 tests (pg/mysql suites opt-in via env DSN)
```

## License

[MIT](LICENSE)
