# openapi-mcp-hub

**Turn any OpenAPI spec — or a live PostgreSQL / MySQL database — into production-ready MCP endpoints. One process. One port. Zero code.**

[![CI](https://github.com/zhangjk1207/openapi-mcp-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/zhangjk1207/openapi-mcp-hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-6e7bf4.svg)](https://github.com/zhangjk1207/openapi-mcp-hub/blob/main/LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://github.com/zhangjk1207/openapi-mcp-hub/blob/main/pyproject.toml)
[![中文文档](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-red.svg)](https://github.com/zhangjk1207/openapi-mcp-hub/blob/main/README.zh-CN.md)

![demo](https://raw.githubusercontent.com/zhangjk1207/openapi-mcp-hub/main/docs/demo.gif)

> Install: `pip install openapi-mcp-hub` or `uvx openapi-mcp-hub` · Python 3.10+ · MIT


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

Public 2026 MCP ecosystem reports keep finding the same pain: only ~8.5% of MCP servers use OAuth, and tool-poisoning / rug-pull changes go unnoticed until an agent misbehaves. That is why this gateway treats **data-plane auth and tool-change visibility as first-class features**, not enterprise add-ons.

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

## Install

```bash
pip install openapi-mcp-hub      # or: uvx openapi-mcp-hub
openapi-mcp-hub --version
```

Container (multi-arch: amd64 / arm64):

```bash
docker run -d --name openapi-mcp-hub -p 20033:20033 \
  -e OPENAPI_MCP_HUB_ADMIN_TOKEN=change-me \
  -v "$PWD/data:/app/data" \
  ghcr.io/zhangjk1207/openapi-mcp-hub:latest
```

### CLI

Every flag is optional — with no arguments the gateway reads `OPENAPI_MCP_HUB_*` environment
variables exactly as before. CLI wins over env.

| Flag | Same env var | Default |
|---|---|---|
| `--host` | `OPENAPI_MCP_HUB_HOST` | `0.0.0.0` |
| `--port` | `OPENAPI_MCP_HUB_PORT` | `20033` |
| `--data-dir` | `OPENAPI_MCP_HUB_DATA_DIR` | `./data` |
| `--database-url` | `OPENAPI_MCP_HUB_DATABASE_URL` | file backend |
| `--host-allowlist` | `OPENAPI_MCP_HUB_HOST_ALLOWLIST` | unrestricted |
| `--docs` | `OPENAPI_MCP_HUB_DOCS=1` | off |
| `--log-level` | — | `info` |
| `--print-config` | — | print effective config (secrets masked) and exit |

`--print-config` exists because "why won't it start" is almost always a config question. It
prints the effective config with credentials masked (never the value itself) and exits without
starting the server. CLI help and log messages are in Chinese; flag names and env vars are stable.

## Quickstart — 60 seconds

```bash
git clone https://github.com/zhangjk1207/openapi-mcp-hub && cd openapi-mcp-hub
docker compose up -d       # gateway + PostgreSQL + demo API, auto-registers a petstore service
```

Open **http://localhost:20033** and paste the demo admin token: `demo-admin-token` — the console shows a ready-to-use `petstore` service (fixed demo key, listed below).

Try the agent-callable endpoint right away:

```bash
curl -X POST http://localhost:20033/mcp/petstore \
  -H "X-Api-Key: psk-demo-petstore-key" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_pets","arguments":{"limit":3}}}'
```

And in Claude Code:

```bash
claude mcp add --transport http petstore http://localhost:20033/mcp/petstore \
  --header "X-Api-Key: psk-demo-petstore-key"
```

> The demo mode (`OPENAPI_MCP_HUB_DEMO=1` in `docker-compose.yml`) uses a fixed, publicly-documented key for a friction-free tour. Turn it off (and change `OPENAPI_MCP_HUB_ADMIN_TOKEN`) before any real use — real registrations always mint a fresh one-time key.

### Register your own API

```bash
ADMIN_TOKEN=demo-admin-token

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

## Connect your agent

Any MCP client that speaks Streamable HTTP works. The endpoint is `http://<host>:20033/mcp/<service>`
and it authenticates with the per-service key in an `X-Api-Key` header (or a JWT `Authorization:
Bearer` when the service runs in OAuth 2.1 resource-server mode).

The JSON shape below is the de-facto common one — clients that use `mcpServers` accept it verbatim:

```json
{
  "mcpServers": {
    "petstore": {
      "url": "http://localhost:20033/mcp/petstore",
      "headers": { "X-Api-Key": "psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" }
    }
  }
}
```

| Client | Where it goes |
|---|---|
| **Claude Code** | `claude mcp add --transport http petstore <url> --header 'X-Api-Key: psk-…'` |
| **Claude Desktop** | `claude_desktop_config.json` → `mcpServers` (needs a version with remote-HTTP support; older builds go through the `mcp-remote` bridge) |
| **Cursor** | `~/.cursor/mcp.json` → `mcpServers` |
| **Cline / Roo** | MCP settings → `mcpServers` (remote server: URL + headers) |
| **VS Code (Copilot agent mode)** | `.vscode/mcp.json` → **`servers`** key, with `"type": "http"` |
| **Dify** | Tools → MCP server: endpoint URL + `X-Api-Key` header |
| **Open WebUI** | Settings → Tools → add MCP server (URL + header) |
| **Anything else** | Any MCP SDK `streamable_http` client, or the legacy SSE endpoint below |

> Field names drift between client versions — where a client differs (VS Code uses `servers`,
> not `mcpServers`), that client's own docs are authoritative. The gateway side never changes:
> one URL, one header.

Legacy SSE clients (older than Streamable HTTP) use the compatibility endpoint with the same key:

```
GET http://localhost:20033/mcp/<service>/sse
```

### stdio-only clients — use the built-in bridge

Some clients can only spawn a local process and talk JSON-RPC over stdin/stdout. The same package
ships a bridge subcommand (`uvx openapi-mcp-hub bridge …`, or `openapi-mcp-hub bridge …` from a
local install) that pipes stdio straight to a running gateway endpoint:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "uvx",
      "args": [
        "openapi-mcp-hub", "bridge",
        "--url", "http://localhost:20033/mcp/petstore",
        "--header", "X-Api-Key=psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      ]
    }
  }
}
```

The bridge is protocol plumbing only: it forwards every message unchanged, keeps `Mcp-Session-Id`
sticky, derives the `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers from the client's own
request, and turns transport-level failures (401, unreachable gateway) into JSON-RPC errors with an
actionable `data.hint`. It does not cache, rewrite or re-implement anything, and it is not needed
when the client can speak Streamable HTTP directly.

<!-- mcp-name: io.github.zhangjk1207/openapi-mcp-hub -->

Verify from the shell without any client:

```bash
curl -s -X POST http://localhost:20033/mcp/petstore \
  -H "X-Api-Key: psk-…" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

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
| **MCP data-plane auth** | Static key **or JWT as an OAuth 2.1 resource server**: external IdP (RS256/ES256 + JWKS + issuer/audience checks, RFC 9728 discovery, `WWW-Authenticate` on 401) or built-in issuance (HS256, short-TTL, `aud` bound to the service) |
| **Tool-poisoning visibility** | Every update/`refresh` returns a tool-set diff (`added` / `removed` / `description_changed` / `input_schema_changed`) plus a human-review warning when descriptions change — descriptions are the LLM's instruction surface |
| **Target API auth** | Bearer / API key / Basic / **OAuth2 client_credentials** — token fetched, cached & auto-refreshed by the gateway (one 401 retry); credentials referenced by env var name only, `token_url` allowlist-checked at registration |
| **Hot registration** | Register / update / delete without restarts; failures roll back atomically; `spec_url` sources re-pull via `refresh` (key preserved) |
| **Filtering** | Path include/exclude globs, tool whitelist, `max_tools` cap, response templates |
| **Persistence** | JSON file or PostgreSQL backend, automatic one-way migration |
| **Console** | Single-file admin UI, zero build chain, zero external assets — works in air-gapped intranets |
| **Agent-facing tooling** | `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` inferred per tool, so agents can auto-run reads and ask before deletes; tool descriptions compacted to ≤320 chars and schema descriptions to ≤160 (measured 11%–75% smaller `tools/list` on verbose specs); `tools/list` declares `ttlMs`/`cacheScope` and a deterministic (read-only-first, then alphabetical) order for prompt-cache hits |
| **Progressive tool discovery** | Services with ≥8 tools get a `search_tools` meta-tool (BM25 over names, paths, descriptions and parameter names — no embeddings, no model calls, no new dependencies). Above `--advertise-limit` (default 50) only the most useful tools are listed and the rest are found by search: a 1000-endpoint API no longer floods the context window |
| **Actionable errors** | Every failure carries a machine-readable `data.hint` telling the agent what to change or whether retrying is pointless (`401` → "server-side, retrying will not help", `422` → "fix the field named in the message", validation → lists legal parameter names) |
| **Observability** | Health probes, in-memory ring call log (last 20/service), call stats (ok-rate / p50 / p95 / top tools), structured error codes (`-32000`/-`32602`/-`32002`) |

## Comparison

| | openapi-mcp-hub | one-MCP-server-per-spec | general API gateways + MCP plugin |
|---|---|---|---|
| New service | POST a spec, ~1s | Write & deploy a wrapper | Plugin config + reload |
| Ports | **1** | N | 1 (heavyweight) |
| Database → MCP | **Built-in, read-only enforced** | Separate project | Rarely |
| Air-gapped console | **Yes, single HTML file** | — | Usually needs internet |
| MCP data-plane auth (OAuth 2.1 resource server) | **Built-in, free** | — | Kong AI Gateway: RBAC/OIDC requires the paid Enterprise tier; Zuplo: SaaS |
| Tool-poisoning visibility | **Tool-set diff + review warning on every update/refresh** | — | Rarely |
| Runtime deps | FastAPI + mcp SDK (~10 packages) | varies | Full gateway stack |

## Security posture

Everything below ships in the free, self-hosted, single-binary build:

| Layer | Mechanism |
|---|---|
| Management plane | `OPENAPI_MCP_HUB_ADMIN_TOKEN` (env only); all admin routes require it |
| Data plane (static mode) | Per-service `X-Api-Key`, shown once, stored as SHA-256 hash, rotatable via `rotate-key` |
| Data plane (JWT mode) | `Authorization: Bearer` — **external IdP**: RS256/ES256 via JWKS (cached 300s, kid rotation aware), `iss`/`aud` enforced, algorithms whitelisted (no alg-confusion); **built-in**: HS256 with `OPENAPI_MCP_HUB_JWT_SECRET` (env only, never persisted), TTL ≤ 24h, `aud` bound to the service name. RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp/{name}` so spec-native clients (e.g. Claude Code) can discover your IdP |
| SSRF | Three layers: registration-time host allowlist, per-hop re-check on redirects, response timeouts |
| Secrets at rest | Target-API credentials referenced by env var *names* only — nothing sensitive is persisted |
| Database tools | Six-layer read-only (see above) |
| Tool poisoning | `tool_changes` diff + human-review warning whenever tool descriptions change on update/refresh |

JWT mode in three commands (built-in issuance):

```bash
export OPENAPI_MCP_HUB_JWT_SECRET="$(openssl rand -hex 32)"
# register with authn={"mode":"jwt"} (see USAGE.md for external-IdP mode)
curl -X POST $GW/services -H "Authorization: Bearer $ADMIN" -d '{"name":"crm","spec_url":"...","base_url":"...","authn":{"mode":"jwt"}}'
curl -X POST $GW/services/crm/token -H "Authorization: Bearer $ADMIN" -d '{"ttl_s":3600}'
# → use the returned token as: Authorization: Bearer <token>
```

## Console screenshots

Real console, not mockups — REST and database sources in one table, generated tool sets,
health probes and key rotation, all from a single static page: no build chain, no external
assets, works air-gapped.

**Services** — REST and database sources side by side, `/mcp/{name}` per row:

![Services list](https://raw.githubusercontent.com/zhangjk1207/openapi-mcp-hub/main/docs/screenshots/console-list.png)

**Service detail** — endpoint, generated tools with descriptions, health probe, key rotation:

![Service detail](https://raw.githubusercontent.com/zhangjk1207/openapi-mcp-hub/main/docs/screenshots/console-detail.png)

**Register a REST API** — spec upload / paste, path filters, target auth by env var name:

![Register a REST API](https://raw.githubusercontent.com/zhangjk1207/openapi-mcp-hub/main/docs/screenshots/console-register-rest.png)

**Register a database** — DSN referenced by env var name only, read-only tool trio generated:

![Register a database](https://raw.githubusercontent.com/zhangjk1207/openapi-mcp-hub/main/docs/screenshots/console-register-db.png)

## Roadmap

- [x] v0.5.0 — **agent-facing tool quality** · tool annotations (`readOnlyHint`/`destructiveHint`) · description/schema compaction · actionable error hints · CLI flags · multi-arch ghcr image
- [x] v0.4.0 — **JWT data-plane auth (OAuth 2.1 resource server, RFC 9728)** · tool-poisoning drift diff on update/refresh
- [x] v0.3.0 — OAuth2 client_credentials target auth · spec refresh endpoint · call stats · console auth fix
- [x] M5-1 — Database → MCP (PostgreSQL + MySQL 8, six-layer read-only)
- [ ] M5-2 — per-table tool mode, sensitive-column masking
- [ ] M5-3 — write operations (opt-in, statement allowlist)
- [ ] Metrics endpoint (Prometheus) & key expiry
- [ ] Per-tenant quotas / rate limiting
- [ ] Per-service circuit breaking

Deliberately **not** on this list: anything that turns the gateway into a platform
(RBAC/SSO, multi-replica coordination, metric stacks, audit stores). Each of those makes
the default path heavier for a problem the reverse proxy in front of you already solves.

**Not implemented today** — do not plan around these: circuit breaking, per-tenant
rate limiting or quotas, HA / multi-replica coordination, Prometheus metrics,
database write operations, per-table database tools, API-key expiry. Safety isolation
today is per-service httpx client + independent timeout + connection-pool cap + the
SSRF allowlist, all inside a single process.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q          # 279 pass; 22 opt-in integration tests skip without PG/MySQL DSNs
```

## License

[MIT](LICENSE)
