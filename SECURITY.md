# Security Policy

## Supported versions

`main` and the latest tagged release receive security fixes. Older tags are not patched.

| Version | Supported |
|---|---|
| 0.4.x | ✅ |
| < 0.4 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability.

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability**
(<https://github.com/zhangjk1207/openapi-mcp-hub/security/advisories/new>).

Include, where applicable: version or commit, run mode (`docker compose`, the
`openapi-mcp-hub` entry point, or from source), Python version, the registration
payload with **all tokens, keys and DSNs redacted**, and a reproducer.

Expect an acknowledgement within **7 days** and a status update within **30 days**.
Fix timelines depend on severity; you will be credited in the advisory unless you ask
not to be.

## In scope

This gateway's security model is: *the admin token is the only door to the management
plane, and target credentials are only ever referenced by environment variable name.*
Anything that breaks one of those invariants is in scope.

- **SSRF allowlist bypass** — DNS rebinding, redirect chains (per-hop re-check),
  IPv4-mapped IPv6 forms, the `spec_url` fetch path, the OAuth2 `token_url` fetch path.
- **Read-only enforcement bypass in the database tools** — escaping the
  `SELECT` / `WITH` / `EXPLAIN` lexical check, defeating the server-side
  `READ ONLY` transaction, defeating the outer `LIMIT` wrap, defeating
  `statement_timeout` / `max_execution_time`, or reaching tables excluded by
  `include_tables` / `exclude_tables`.
- **Credential exposure** — a `*_env` value reaching disk, logs, error responses, the
  admin API, health-probe output, or the console.
- **Data-plane authentication bypass** — `X-Api-Key` validation, hashed-key storage,
  key rotation, and JWT verification (algorithm confusion, `alg: none`, `kid` handling,
  JWKS caching, `iss` / `aud` / `exp` enforcement, `aud`-to-service binding).
- **Management-plane bypass** — reaching any `/services/*` route without the admin token.
- **Tool-poisoning visibility gaps** — a change to a tool's `description` or
  `input_schema` that does not surface in `tool_changes` on update/refresh.
- **DoS through a single registered service** — crashing or blocking the gateway or
  other services via one service's spec, tool call, or upstream response.

## Out of scope

- **Demo mode.** `OPENAPI_MCP_HUB_DEMO=1` publishes a fixed petstore key
  (`psk-demo-petstore-key`) and the sample compose file ships a default admin token
  (`demo-admin-token`). Both are **deliberately public** and documented in the README.
  Running demo mode in an exposed environment is misconfiguration, not a vulnerability.
- **Missing TLS.** The gateway speaks plain HTTP by design; terminate TLS in front of it.
- **A single shared admin token.** There is no per-user identity, RBAC, or admin audit
  log yet — that is a documented design decision, not an oversight.
- **Missing rate limiting / circuit breaking.** Not implemented yet; see the roadmap.
  Upstream abuse from a legitimately registered service is out of scope.
- **Anything requiring an operator to have deliberately widened
  `OPENAPI_MCP_HUB_HOST_ALLOWLIST`, disabled the allowlist, or exported a credential
  into the demo/test environment.**
- **Vulnerabilities in the target APIs themselves**, or in a spec that the operator
  chose to register.
- Automated scanner output with no demonstrated impact.

## Hardening checklist for operators

1. Set `OPENAPI_MCP_HUB_ADMIN_TOKEN` to a long random value; never reuse the demo one.
2. Never run with `OPENAPI_MCP_HUB_DEMO=1` outside a throwaway local sandbox.
3. Keep `OPENAPI_MCP_HUB_HOST_ALLOWLIST` as tight as your upstreams allow.
4. Put TLS and network access control in front of the gateway; expose `/mcp/*` only to
   the agents that need it.
5. Give database accounts `SELECT`-only grants on the schemas you actually need, even
   though the gateway enforces read-only itself.
6. Treat tool descriptions from third-party specs as untrusted input — that is why
   `tool_changes` exists.
