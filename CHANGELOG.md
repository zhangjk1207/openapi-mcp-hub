# Changelog

## 0.4.1 (2026-10-26)

First release on PyPI. No behavior changes — packaging, docs and metadata only.

### Added

- **PyPI release pipeline** (`.github/workflows/publish.yml`): builds sdist + wheel,
  runs `twine check --strict`, asserts `pyproject.toml` version == release tag, smoke-tests
  the built wheel in a clean venv, then publishes via PyPI Trusted Publishing (OIDC, no
  stored token). `pip install openapi-mcp-hub` / `uvx openapi-mcp-hub` now work.
- **`SECURITY.md`**: reporting channel (private advisories), supported versions,
  explicit in-scope list (SSRF allowlist, read-only enforcement, credential exposure,
  data/management-plane auth, tool-poisoning visibility) and out-of-scope list
  (demo-mode fixed credentials, missing TLS, single shared admin token, missing
  rate limiting), plus an operator hardening checklist.
- **`Makefile`**: `make install / test / lint / check / build / clean`. Ruff rules live in
  `pyproject.toml`, so local and CI runs use the same standard.
- **Console screenshots** in both READMEs (`docs/screenshots/`), replacing the
  "screenshots coming with the first tagged release" placeholder left over since v0.2.1.
- **Test**: `tests/test_version.py` asserts `__version__` matches `pyproject.toml`.

### Changed

- Packaging metadata completed for PyPI: English summary, `keywords`, `classifiers`,
  `[project.urls]` (Homepage / Repository / Issues / Changelog / Documentation), and
  PEP 639 license expression (`license = "MIT"` + `license-files`); build requires
  `setuptools>=77`.

### Fixed

- **`README.zh-CN.md` claimed circuit breaking and per-service rate limiting**, neither of
  which exists in the code — contradicting the English README, which correctly lists rate
  limiting as roadmap. Architecture diagram, isolation notes and the project-structure
  annotations now describe what actually ships (per-service httpx client, independent
  timeout, connection-pool cap, one 401 retry for OAuth2).
- Both READMEs now state explicitly what is **not** implemented (circuit breaking, quotas /
  rate limiting, HA, Prometheus metrics, database writes, per-table tools, key expiry)
  instead of leaving it to be inferred from unchecked roadmap boxes.
- README test count corrected (`164` → `177 pass, 22 opt-in integration tests`).

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
