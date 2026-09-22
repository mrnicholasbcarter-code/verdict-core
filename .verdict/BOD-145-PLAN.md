# BOD-145 Story Plan — SharedMemoryProvider (MEMORY M1)

Base: a7d0eecd (main). Worktree: .worktrees/bod-145-shared-memory-provider
Branch: feat/bod-145-shared-memory-provider
Authority: Linear BOD-145 + ADR-004/009; new ADR-033.

## Goal
Add a versioned `SharedMemoryProvider` boundary + canonical `SharedMemoryEnvelope`
and a production adapter for `doobidoo/mcp-memory-service` (SQLite-vec + local
embeddings). Optional/replaceable. Remote/shared records normalize into
`MemoryRecord` with `authority_verified=false` by default. Extend setup/doctor
certification states (`not-installed` … `incompatible`). No silent install.
Secrets never in receipts/logs. Core tests must not require Internet.

## Non-goals / authority preserved
- MemoryPlane remains local SoT (ADR-004/009). Shared provider is advisory ACL.
- No coupling to SQLite-vec specifics; first backend only.
- No generative LLM for health/store/search.
- No Milvus/Qdrant; no harness hook rewrites beyond provider/config surface.
- No silent third-party install.

## HEAD modules discovered
- `verdict/memory_plane.py` — `MemoryRecord` (`authority_verified: bool = False`)
- `verdict/memory_adapters.py` — `MemoryAdapter`, `ContextProviderResult`,
  `ProviderResultStatus`, `_redact`, `content_hash`
- `verdict/memory_bridge.py` — `configure_memory_bridge`, `run_doctor_diagnostics`
- `verdict/context_sources.py` — `NativeMemoryProvider` (local plane consumer)
- `verdict/cli.py` — `cmd_doctor` already merges `run_doctor_diagnostics`

## Design

### 1. New modules (preferred)
- `verdict/shared_memory.py` — envelope, query/hit/ref, ProviderHealth,
  SharedMemoryProvider Protocol, FakeSharedMemoryProvider, normalize →
  MemoryRecord, certification states, setup discovery, doctor report, redaction.
- `verdict/shared_memory_mcp.py` — MCP Memory Service HTTP adapter with injectable
  transport, timeout/auth/error taxonomy, no generative dependency.

### 2. Canonical envelope (`shared-memory-envelope/v1`)
Fields: schema_version, protocol_version, content_hash, idempotency_key,
tenant, project, scope, source_harness, source_agent, source_session,
memory_kind, content, payload, created_at, observed_at, expires_at,
retention_class, sensitivity, trust, authority, authority_verified,
provenance, revision, classification (`source`|`derived`), derived_from,
metadata (extension namespace only).

Digest: sorted JSON over logical identity fields (exclude volatile observed_at
ingestion clock when marked volatile). Equivalent reordered metadata → same hash.

### 3. Provider contract
```
health() -> ProviderHealth
put(envelope) -> ExternalMemoryRef
search(query) -> SharedMemorySearchResult  # status + hits; empty ≠ unavailable
delete(ref) -> None | capability omission
```
Error taxonomy: not_configured, auth_failed, unreachable, timeout, rate_limited,
schema_incompatible, protocol_incompatible, invalid_request, server_error,
degraded, unknown.

Certification states: not-installed, installed, configured, authenticated,
reachable, healthy, degraded, unavailable, incompatible.

### 4. MCP adapter
REST paths used (anti-corruption, not Core schema):
- GET `/api/health` (or `/health`)
- POST `/api/memories`
- POST `/api/memories/search`
- DELETE `/api/memories/{id}` when supported
Auth: `Authorization: Bearer <token>` from env `VERDICT_SHARED_MEMORY_TOKEN`
or config reference; never materialize into doctor/receipts.
Default timeout bounded; TLS verify on by default.

### 5. Normalization
`normalize_hit_to_memory_record(hit)` always sets `authority_verified=False`,
`authority="shared-memory-advisory"`, provenance includes provider_id + remote ref.
Caller metadata cannot grant project/tenant access.

### 6. Setup / doctor
- `discover_shared_memory_setup()` — reuse existing endpoint/config; never install.
- `doctor_shared_memory_report()` — endpoint (redacted), provider id/version,
  auth state, health, backend type; secrets redacted.
- Wire into `run_doctor_diagnostics` under `shared_memory` key.

### 7. Tests (`tests/test_shared_memory_provider.py`)
AC1 envelope roundtrip + digest stability
AC2 fake + MCP adapter contract suite
AC3 no network / no generative dependency
AC4 project/scope filter (fake + optional live env gate)
AC5 authority_verified is False after normalize
AC6 incompatible schema/version named states
AC7 timeout/unavailable vs empty
AC8 setup discovery no auto-install
AC9 doctor output contract + redaction
AC10 canary secret never appears
AC11 offline fixtures only

### 8. Docs
- ADR-033 SharedMemoryProvider + envelope + authority boundary
- Guide: config example with secret refs, state table, TLS/VPS guidance

### 9. Rollback
Delete new modules/docs/tests; revert doctor wiring. Local MemoryPlane unaffected.

## Proof gates
STATIC / UNIT / INTEGRATION / ACCEPTANCE-PROOF as in packet.
