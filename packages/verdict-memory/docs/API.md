## SECURITY HOLD (parent 2026-10-16)
**STOP live backend writes/proof** until Sol backend-security findings are fixed in ONE bounded pass.
Provisional `AiMemoryClient` is **NOT safe** for E/D consume or live install.
Event contracts (`LifecycleEvent` / vectors / FakeProvider / outbox enqueue semantics) may continue.
Findings (c92efa4e mock repros): foreign query overwrites configured project; unscoped hit relabel;
distinct harness/session share remote raw source-id path; empty write RPC accepted without persisted digest;
nested metadata secrets durable clear; malformed RPC `{}` → available empty; proof any-hit not exact ref/digest.

# BOD80 P — provisional public API (experimental 0.1.0a1)

**Status:** experimental / compatible within `lifecycle-event/v1`. Not a frozen 1.0.
**Owner:** P (`packages/verdict-memory/**` only). E owns Verdict consumer + root dependency.
**Base:** `536b1a316c8cfaf03799bf8779be32d47ff41746` · branch `feat/bod80-memory-package`
**Worktree:** `/home/nick/worktrees/bod80-memory-package`
**Package:** `packages/verdict-memory` · import `verdict_memory`

## Version / compatibility policy
- Package version: `0.1.0a1` (alpha). Schema family `lifecycle-event/v1`.
- **Unknown optional fields:** ignored on read (`LifecycleEvent.from_dict` filters to documented known fields). Additive only; no generic framework.
- **Unsupported `kind`:** explicit `ValueError("unsupported event kind: …")` after alias normalization — never silent success.
- Breaking schema changes bump schema id (`lifecycle-event/v2`).
- No unearned 1.0 freeze until a real vertical + golden vectors validated with native pair.

## Canonical event kinds (BOD-80 ticket list)
`session_start`, `before_first_model_turn`, `before_tool`, `after_tool`, `after_edit`,
`subagent_start`, `subagent_end`, `before_compact`, `before_yield`, `verification_result`,
`session_end`, `resume`.

Package-level kinds also allowed: `handoff`, `manual_capture`.

Provisional aliases (normalized at construction):
- `before_turn` → `before_first_model_turn`
- `compaction` → `before_compact`

## Language-neutral seam (JSON)
Serialization: `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` → UTF-8 → SHA-256.

Logical digest **excludes** `observed_at` and `deadline_at`. Prefer harness-supplied `identity.source_event_id` for stable replay/compaction dedupe (not adapter wall-clocks).

Golden vectors:
- `vectors/lifecycle_event_v1_minimal.json` (Unicode content + source_event_id)
- `vectors/lifecycle_event_v1_unicode_optional.json`
- `vectors/lifecycle_event_v1_numeric.json` (pinned float spellings e.g. `1700000000.0`, `1.0`)

Bundled in the wheel under `verdict_memory/vectors/`.

Same-byte parity set (Rust/Python): `vectors/golden/lifecycle-event-v1/` with exact canonical, digest-payload, fingerprint and idempotency bytes per case, `SHA256SUMS`, and rejected inputs. The digest payload is the canonical object minus `content`, `observed_at`, `deadline_at` and `idempotency_key`.

### LifecycleEvent fields
Documented known fields (unknown extras ignored on read):
`schema_version`, `protocol_version`, `kind`, `content`, `content_hash`, `identity`,
`created_at`, `observed_at`, `scope`, `memory_kind`, `sensitivity`, `trust`,
`classification`, `derived_from`, `provenance`, `payload`, `metadata`, `revision`,
`idempotency_key`, `deadline_at`.

Identity known fields: `source_harness`, `project`, `session_id`, `parent_session_id`,
`attempt_id`, `source_event_id`, `tenant`, `workspace`, `source_agent`.

**Authority rule:** harness identity is provenance only. Auth/project authority comes from authenticated client **config**, never from free-form event payload.

## Python public API
```python
from verdict_memory import (
    LifecycleEvent,
    CaptureDecision,
    RecallQuery,
    RecallResult,
    FakeMemoryProvider,
    MemoryCapturePolicy,
    MemoryProvider,
    ProviderResultStatus,
    ProviderHealth,
    ProviderErrorCode,
    CANONICAL_EVENT_KINDS,
    ALLOWED_EVENT_KINDS,
    EVENT_KIND_ALIASES,
    canonicalize,
    event_digest,
    loads_lifecycle_event,
    redact_secrets,
    CONTRACT_API_VERSION,
    LIFECYCLE_EVENT_SCHEMA,
)
from verdict_memory.contracts import EventIdentity
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
```

Entrypoints: normalize → capture decision → enqueue → recall.

## Packaging
Hatchling src-layout. Wheel **must** contain `verdict_memory/*.py` modules + bundled vectors.
Regression: `tests/test_packaging_wheel.py` builds a wheel, asserts module files present,
installs into an isolated venv with empty `PYTHONPATH`, imports from outside the checkout.

## Backend client
`AiMemoryClient` talks to existing ai-memory 2.6.6 MCP (`Accept: application/json, text/event-stream`).
Token from file path only; never print. Live round-trip proof continues separately.
Do **not** revive `AiMemoryExperimentalProvider` (D27/#893).

## What E needs
- Path/wheel dep on `packages/verdict-memory` (E owns root dependency line).
- Adapt Verdict `MemoryRecord` → `LifecycleEvent` at consumer boundary (no `verdict.*` imports in package).
- Report actual adapter needs against this experimental manifest; do not invent 1.0 support claims.


## source_event_id / idempotency (updated)
- When `identity.source_event_id` is set, `idempotency_key = source_idempotency_key(...)`
  namespaced by `(schema, protocol, source_harness, project, workspace, tenant, session_id, source_event_id)`.
      Same `source_event_id` in a different session does not collide. Auth client config project/workspace
      is authority for remote writes — event.identity.project is provenance only and must match config.
  **Excludes** `created_at`, `observed_at`, content, and adapter clocks.
- `event.digest()` remains the **content fingerprint** (includes `created_at` + content_hash; excludes observation clocks).
- Outbox: same key + same content fingerprint → return existing row (**original `created_at` preserved**).
  Same key + different content fingerprint → `IdempotencyConflictError` (no silent second memory, no silent replace).
- Without `source_event_id`, enqueue key falls back to content digest (created_at is part of that fingerprint).
- Receipt/injection observation time must not be used as source creation time.
- Rust numeric/Unicode vector parity is **unmeasured** until D reports; Python vectors are fixtures only.

## Security contract (9f1a54f4 — Sol R1–R5 fix pass)
- **Config authority:** `AiMemoryClientConfig.project/workspace` constrain put/search/delete.
  Foreign `RecallQuery.project/workspace` → typed error **before RPC**. Event identity project/workspace
  mismatch → `INVALID_REQUEST`.
- **Unscoped hits:** missing project/workspace on hits are **skipped** (not relabeled as config project).
- **Remote path:** `verdict-memory/{kind}/{idempotency_key}.md` (full namespaced key, not raw truncated source id).
- **Write ACK:** requires non-empty acknowledged `path` from tool result; empty `{}` → `MALFORMED` (no invented ack).
- **Search schema:** missing `hits`/`results` or malformed RPC → `MALFORMED` (not AVAILABLE empty).
- **Redaction:** `redact_secrets` + `redact_mapping` before remote put and durable outbox (nested metadata/payload/provenance).
  Capture policy gates direct put (secrets/private sensitivity denied).
- **Delete:** only paths under `verdict-memory/`; arbitrary paths → `INVALID_REQUEST`.
- **Endpoint:** loopback HTTP or HTTPS; reject URL credentials/query/fragment/non-loopback HTTP.
- **Live install:** HOLD until parent clears after mock re-run. E may use Fake/serialization only until then.
