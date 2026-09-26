# Shared Memory Provider

Optional, advisory network memory for Verdict harnesses (the shared memory provider (ADR-033) / MEMORY M1).

Local `MemoryPlane` remains the durable authority. Shared hits normalize into
`MemoryRecord` with `authority_verified=false` and
`authority="shared-memory-advisory"`.

## Configure (no silent install)

```bash
export VERDICT_SHARED_MEMORY_URL="https://memory.example"
export VERDICT_SHARED_MEMORY_TOKEN="..."   # never commit; never paste into receipts
```

Discovery reuses an existing MCP Memory Service instance. Verdict never
auto-installs or mutates third-party software during `setup` / `doctor`.

## Certification states

| State | Meaning |
| --- | --- |
| `not-installed` | No endpoint and no local MCP Memory Service markers |
| `installed` | Binary/MCP registry marker present, endpoint not configured |
| `configured` | Endpoint present (token may still be missing) |
| `authenticated` | Token present and accepted |
| `reachable` | Network path answers |
| `healthy` | Health check passed |
| `degraded` | Reachable but reported degraded |
| `unavailable` | Configured but unreachable/timeout/auth failure |
| `incompatible` | Schema/protocol mismatch |

## Doctor

```bash
verdict doctor --json
```

Nested key `shared_memory` reports redacted endpoint, provider id/version,
auth state, health, and backend type. Secrets and bearer tokens never appear.

## TLS / VPS notes

- TLS verification is on by default in the HTTP adapter.
- Prefer reverse-proxied HTTPS with short-lived tokens.
- Empty search results are success; timeout/unavailable/auth failures are
  distinct named statuses.
