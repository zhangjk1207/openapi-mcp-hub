# Changelog

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
