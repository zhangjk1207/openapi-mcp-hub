# Changelog

## 0.4.0 (2026-10-14)

### Added

- **JWT data-plane auth — MCP OAuth 2.1 resource server role.** Per-service `authn`
  registration field replaces the static-key-only posture (static remains the default,
  fully backward compatible). Two modes:
  - **External IdP**: `jwks_url` + `issuer` (+ optional `audience`); tokens verified via
    JWKS (cached 300 s, kid-rotation aware, fetched off the event loop), signature
    algorithms whitelisted (`RS256`/`ES256`, no alg-confusion), `iss`/`aud` enforced,
    `exp`/`iat` required. RFC 9728 protected-resource metadata served at
    `/.well-known/oauth-protected-resource/mcp/{name}` so spec-native clients (Claude
    Code, MCP SDK clients) can discover the enterprise IdP; `401`s carry
    `WWW-Authenticate` with a `resource_metadata` pointer.
  - **Built-in issuance**: `{"mode":"jwt"}` without `jwks_url`; gateway signs short-TTL
    HS256 tokens (`POST /services/{name}/token`, TTL clamped to 60 s–24 h, `aud` bound
    to the service name, `jti` for log correlation) with `OPENAPI_MCP_HUB_JWT_SECRET`
    (env only, never persisted). Registration responses in JWT mode no longer return a
    (non-functional) `api_key`.
  In JWT mode the `X-Api-Key` channel is closed for that service. New dependency:
  `PyJWT[crypto]`.
- **Tool-poisoning visibility**: `PUT /services/{name}` and `POST /services/{name}/refresh`
  now return `tool_changes` — a four-way diff (`added` / `removed` / `description_changed` /
  `input_schema_changed`) between the old and new tool sets. Description changes raise an
  explicit human-review warning (descriptions are the LLM's instruction surface;
  silent upstream changes are the classic rug-pull/tool-poisoning vector).

## 0.3.0 (2026-10-13)

### Added

- **OAuth2 `client_credentials` target auth** — new `auth.type: "oauth2"`
  (`token_url` / `client_id_env` / `client_secret_env` / optional `scope` / `scope_env`).
  The gateway fetches, caches and auto-refreshes tokens (60s early expiry,
  double-checked lock so concurrent calls trigger a single token request);
  one 401 from the target API forces a refresh and retries once. `token_url`
  passes the SSRF allowlist at registration time; secrets are referenced by
  env var name only and never persisted or logged.
- **`POST /services/{name}/refresh`** — re-pull a `spec_url` source and
  hot-update the service (`api_key` and `created_at` preserved). The original
  `spec_url` is now persisted; startup restore still uses the local spec
  snapshot, so restarts never depend on external reachability.
- **`GET /services/{name}/stats`** — aggregated view over the in-memory call
  ring buffer: ok-rate, p50/p95 duration, per-tool top list
  (last 200 calls, reset on restart).

### Fixed

- **Console auth form**: the `api_key` type sent a nonexistent `api_key_env`
  field (silently dropped by pydantic → every call failed with `-32001`);
  `basic` was missing from the dropdown. Both fixed; OAuth2 added with
  conditional `token_url` / `scope` fields.

### Housekeeping

- Dockerfile `HEALTHCHECK` against `/healthz` (no token required).
- Test suite: 163 passed (+13 covering OAuth2 token lifecycle, 401 retry,
  refresh semantics, stats aggregation, allowlist enforcement).

## 0.2.1 (2026-10-12)

- CONTRIBUTING.md, bug/feature issue templates, first tagged release.

## 0.2.0

- One-command demo (`docker compose up` auto-registers a playable petstore
  service), console visual polish, CI on Python 3.10–3.12 with PostgreSQL and
  MySQL services.
