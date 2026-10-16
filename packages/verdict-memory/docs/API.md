# BOD80 P — provisional public API (experimental 0.1.0a1)

**Status:** experimental / compatible within `lifecycle-event/v1`. Not a frozen 1.0.
**Owner:** P (`packages/verdict-memory/**` only). E owns Verdict consumer + root dependency.
**Base:** `536b1a316c8cfaf03799bf8779be32d47ff41746` · branch `feat/bod80-memory-package`
**Worktree:** `/home/nick/worktrees/bod80-memory-package`
**Package:** `packages/verdict-memory` · import `verdict_memory`

## Version policy
- Package version: `0.1.0a1` (alpha). Schema family `lifecycle-event/v1`.
- Breaking schema changes bump the schema id (`lifecycle-event/v2`); additive fields allowed in v1 with unknown-field ignore on readers.
- No unearned 1.0 freeze until a real vertical + golden vectors validated with native pair.

## Language-neutral seam (JSON)
Serialization (existing Verdict shared-memory convention):
`json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` → UTF-8 → SHA-256.

Logical digest **excludes** `observed_at` and `deadline_at` so the same source event after compaction/resume enqueues once.

Golden vector: `packages/verdict-memory/vectors/lifecycle_event_v1_minimal.json`

### LifecycleEvent (schema `lifecycle-event/v1`)
Required fields:
- `schema_version`, `protocol_version` (`"1"`)
- `kind`: `session_start|before_turn|after_tool|compaction|resume|session_end|handoff|manual_capture`
- `content`, `content_hash` (sha256 of UTF-8 content)
- `identity`: `{source_harness, project, session_id, parent_session_id?, attempt_id?, tenant, workspace, source_agent}`
- `created_at` (float unix seconds)
- `scope`, `memory_kind`, `sensitivity`, `trust`, `classification` (`source|derived`)
- `idempotency_key` (= logical digest if omitted)
Optional: `observed_at`, `deadline_at`, `derived_from[]`, `provenance{}`, `payload{}`, `metadata{}`, `revision`

**Authority rule:** harness identity is provenance only. Auth/project authority comes from authenticated client **config**, never from free-form event payload.

### CaptureDecision
`{eligible: bool, reason: str, event_digest: str|null}`
Stable reasons: `eligible`, `sensitivity_denied`, `secret_detected`, `low_value_tool_noise`, `capture_disabled`.

### RecallQuery / RecallResult / MemoryHit
- Query: `{query, project, scope, tenant, workspace, session_id?, limit, deadline_at?}`
- Result: `{status, hits[], error_code?, message?, capability}`
- Hit: `{ref, event, score, untrusted: true}` — recall is always untrusted / non-authority.
- `status=available` + empty hits = healthy empty (not failure).
- Failed statuses never carry hits.

### ProviderHealth / capability matrix values
`supported|partial|unsupported|unknown` on health/recall results.

## Python public API (`verdict_memory`)
```python
from verdict_memory import (
    LifecycleEvent, CaptureDecision, RecallQuery, RecallResult,
    FakeMemoryProvider, MemoryCapturePolicy, MemoryProvider,
    ProviderResultStatus, ProviderHealth, ProviderErrorCode,
    canonicalize, event_digest, loads_lifecycle_event, redact_secrets,
    CONTRACT_API_VERSION, LIFECYCLE_EVENT_SCHEMA,
)
from verdict_memory.contracts import EventIdentity
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig  # skeleton
```

Entrypoints for consumers (E / adapters):
1. **normalize** — `LifecycleEvent.from_dict` / `loads_lifecycle_event`
2. **capture decision** — `MemoryCapturePolicy.evaluate(event)`
3. **enqueue** — `MemoryOutbox.enqueue(event)` (redacts before durable write; INSERT OR IGNORE)
4. **recall** — `provider.search(RecallQuery(...))` → `RecallResult`

## Outbox bounds (operational SQLite only)
- Table: `memory_lifecycle_outbox`
- States: `pending|acked|dead_letter`
- `DEFAULT_DUE_LIMIT = 20` (matches existing Verdict due-query)
- `DEFAULT_MAX_PENDING = 10_000` (soft refuse with `queue_full`; rationale: protect disk, not an SLA invent)

## Backend client (in progress)
- Target: existing ai-memory **2.6.6** at `http://127.0.0.1:49374` (MCP `/mcp`).
- Token file: `~/.config/ai-memory/auth-token` (load in-process only; never print / never controller env).
- `AiMemoryClient` / `AiMemoryClientConfig` skeleton shipped; live put/search/health probe in next commits.
- Do **not** revive `AiMemoryExperimentalProvider` (D27/#893).

## What E needs to start
- Depend on path package temporarily: `packages/verdict-memory` (path or wheel).
- Adapt Verdict `MemoryRecord` → `LifecycleEvent` at the consumer boundary (package has **no** `verdict.*` imports).
- Launch-gate health check uses `provider.health()`; refuse **new** work on UNAVAILABLE/AUTH_FAILED; keep running work via outbox.
- Request root `pyproject` dependency line via parent when ready for locked artifact.

## Explicit non-goals (P)
- No new retrieval DB/service; no MemoryPlane extraction; no ExperimentalProvider revival.
- No root `verdict/*` or root `pyproject` edits (E owns).
- No publishing / new remotes until parent-approved release.
