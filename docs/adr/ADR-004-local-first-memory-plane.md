# ADR-004: Local-First Memory Plane

- **Status:** accepted; implementation incrementally delivered under #130
- **Date:** 2026-07-27
- **Deciders:** Verdict Core maintainers
- **Related:** [#121](https://github.com/mrnicholasbcarter-code/verdict-core/issues/121), [#126](https://github.com/mrnicholasbcarter-code/verdict-core/issues/126), [#127](https://github.com/mrnicholasbcarter-code/verdict-core/issues/127), [#130](https://github.com/mrnicholasbcarter-code/verdict-core/issues/130)

## Context

Verdict currently has several optional memory and retrieval integrations, but
no portable local authority for durable context. A memory system must remain
useful when networking, hosted models, Ruflo, RuVector, or OpenViking are
absent. Retrieved content is advisory evidence and must never become routing,
authorization, or policy authority.

## Decision

Introduce a versioned `MemoryPlane` contract with SQLite as the durable source
of truth and FTS5 as the deterministic lexical index. External systems are
adapters, not dependencies or authorities. The first contract owns:

- typed records with namespace and scope isolation;
- source/provenance, content hash, authority, confidence, sensitivity,
  timestamps, schema version, expiry, and explicit supersession metadata;
- append-only record history with active-record lookup and contradiction links;
- deterministic lexical retrieval with explicit empty, stale, and unavailable
  states;
- portable, canonical import/export manifests with idempotent content-hash
  deduplication;
- safe adapter boundaries that reject untrusted paths by default and redact
  credentials and raw prompts.

The reference implementation must not make network calls, invoke host CLIs,
read private third-party databases, or require model assets. Semantic
retrieval and provider-backed extraction are optional capabilities whose
absence is explicit and cannot block local writes, reads, or export.

Caller-supplied authority is metadata, not proof. Adapters must derive or
verify authority from their authenticated/documented boundary and mark
unverified input accordingly. A memory result may inform a caller but cannot
grant permissions or bypass Verdict gates.

## Scope and migration

The initial implementation is process-safe and restart-safe for one explicit
SQLite path. It does not silently discover or merge cwd-local Ruflo,
AgentDB, RuVector, OpenViking, Codex, Claude, or Pi stores. Importers are
separate, versioned adapters and begin with dry-run manifests before commit.
The provider-neutral document and JSONL session adapters in #130 accept only
explicit caller paths, apply bounded reads and symlink/path policy, redact by
default, and emit deterministic manifests. The default registry reports
available, degraded, unavailable, and unknown capabilities and isolates
adapter/record failures in aggregate reports. Session records expose an
explicit conversion to the canonical `MemoryRecord` shape; adapters do not
write to the plane automatically. MasterDocsRAG and Code Review Graph private
SQLite inputs are explicitly unavailable through the default registry: callers
must supply validated exported manifests. Provider-specific Claude, Codex, and
Pi JSONL descriptors are explicit format capabilities. Receipts, semantic
indexing, and runtime daemon ownership remain follow-up slices (#117, #121,
#129).

### MasterDocs canonical migration (#127)

The legacy MasterDocsRAG SQLite store is migrated through an explicit,
versioned importer (`MasterDocsAdapter`, `verdict/memory_masterdocs_adapter.py`)
that emits deterministic canonical chunks and a machine-readable report. It is
a legacy optional source, never a MemoryPlane authority. The importer:

- is **versioned**: adapter version `2`, schema version `1`, both recorded in
  every `MasterDocsIngestionReport`. Records and report are deterministically
  hashed (`manifest_hash`) so a pass is reproducible;
- opens the source database **read-only** (`file:<path>?mode=ro`,
  `PRAGMA query_only=ON`) and never mutates it; raw private SQLite inputs are
  disabled by default and require an explicit `allow_legacy_sqlite` opt-in,
  with validated exported manifests preferred;
- **rejects `corpus_fts`** with `status="rejected"` and reason
  `"corpus_fts requires an explicit versioned migration"`; it accepts only the
  `documents` or `doc_fts` tables and `path`+`content` columns;
- applies a **path and quarantine policy**: absolute allowlisted roots, symlink
  rejection, bounded reads (`DEFAULT_MAX_CONTENT_BYTES`, `limit`), and
  quarantine of cache/generated/temp/vendor path components;
- uses a **deterministic replay timestamp**: `ingest_timestamp` defaults to
  `0.0` and is never inferred from the host clock; an explicit non-negative
  value may be supplied for wall-clock provenance;
- marks imported content as **untrusted authority** (advisory evidence, not
  permissions/routing/policy) and writes only to a caller-owned `MemoryPlane`;
  adapters never write to the plane automatically;
- exposes a **dry-run** and CLI surface (`verdict memory masterdocs --dry-run
  --limit --ingest-timestamp --json --allow-legacy-sqlite`); dry-run emits the
  report bundle without writing, and non-dry-run imports via `canonicalize_db`.

This slice makes the MasterDocs boundary explicit and auditable; it does not
claim the issue complete. Future versioned migrations (e.g. for the `corpus_fts`
schema) and broader legacy-store coverage remain follow-up work.

## Consequences

Positive:

- offline baseline retrieval and persistence have one predictable contract;
- provenance and scope remain attached to every item;
- optional integrations can degrade without taking memory offline;
- exported manifests can be inspected, hashed, replayed, and migrated;
- the MasterDocs importer makes legacy migration explicit, read-only,
  deterministic, and manifest-first rather than a silent authority merge.

Negative:

- lexical retrieval is intentionally weaker than semantic retrieval until an
  optional local model adapter is added and benchmarked;
- adapter coverage is incremental and cannot be claimed complete in this
  slice; registry capability status is the source of truth for unsupported
  provider/database formats;
- SQLite path ownership and backup/retention policy require operator config;
- the `corpus_fts` MasterDocs shape remains unsupported pending a future
  versioned migration descriptor.

## Verification requirements

Before calling the MemoryPlane complete, verify restart persistence, TTL and
stale handling, deterministic ordering, namespace isolation, supersession and
contradiction behavior, concurrent readers, redaction, path/size/symlink
policy, corrupt manifests, offline operation, and deterministic
export/import round trips. For the MasterDocs importer specifically, verify
read-only access (no source mutation), `corpus_fts` rejection, quarantine
behavior, deterministic `manifest_hash` over a fixed `ingest_timestamp`,
dry-run-no-write semantics, and non-dry-run write counts and duplicate counts.
Publish a redacted schema/provenance report and an offline smoke transcript.
Any unavailable graph, hosted model, or adapter is recorded as
unknown/unavailable rather than green.
