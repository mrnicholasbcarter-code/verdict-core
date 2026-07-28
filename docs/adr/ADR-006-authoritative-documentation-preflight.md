# ADR-006: Authoritative documentation preflight before implementation

- **Status:** Accepted — implemented in #152/PR #154 and reconciled by #156; integrity hardening tracked by #158
- **Date:** 2026-07-28
- **Decision owners:** Verdict Core maintainers

## Context

The autonomous-development policy requires current, source-attributed
documentation before design and implementation, but the existing document
adapter only emitted unverified records and lifecycle hooks only wrote
receipts. A zero-record shared `~/.verdict/memory.db` therefore did not block
implementation or explain what was missing. Ruflo and RuVector also expose
multiple nested ADR trees, legacy projections, and remote authoritative
sources whose versions can drift independently.

## Decision

Verdict adds a deterministic documentation preflight at the MemoryPlane
boundary. The preflight:

1. discovers the project docs plus explicitly configured or documented local
   Ruflo and remote RuVector sources;
2. resolves remote refs to immutable commits and inventories Markdown ADR
   paths recursively across `docs/adr`, `docs/adrs`, and `implementation/adrs`;
3. reads local Git content at its resolved commit or fetches allowlisted HTTPS
   content; and
4. stores verified document chunks and a manifest in the shared
   `~/.verdict/memory.db`.

Every record carries repository/source URL, ref and commit, relative path,
retrieval time, freshness deadline, raw bytes hash, normalized document hash,
chunk hash, tree/blob SHA when available, and preflight/schema versions.
Records use the `authoritative` trust boundary and can only be written as
verified through `MemoryPlane.put_verified`; normal callers remain unable to
self-assert authority.

## Freshness and failure policy

The default freshness window is 24 hours. A manifest is fresh only when its
verified authority, commit, repository, expiry, and current source content
hash all match. Missing, stale, changed, malformed, truncated, unauthorized,
or unverifiable source state is `blocked`/`unknown`, never healthy. `--fix`
may fetch and ingest the source, but a partial repair does not unlock
implementation. Re-running an unchanged source is idempotent.

## Lifecycle enforcement

The implementation variants of `MemoryHookController.on_task_start` and
`on_file_edit_start` call `require_documentation_preflight` before receipts or
edits proceed. A failed preflight raises `DocumentationPreflightError` and
fails closed. Non-implementation context capture remains compatible with the
existing hook matrix.

`verdict doctor --fix` reports preflight status and repairs missing/stale
documents. `verdict memory docs [--fix]` exposes the same machine-readable
report for automation.

## Source allowlist and privacy

Remote fetching is limited to the configured authoritative repository API/raw
URLs. Local sources must be explicit repository roots. Credentials,
authorization headers, private URLs, prompts, and provider responses are never
stored. Runtime memory and generated evidence are local state and are not
committed.

When no local Ruflo checkout is available, the preflight discovers Ruflo through
the allowlisted GitHub repository API and raw-content endpoints, resolves the
requested ref to an immutable commit before inventory, and inventories the
remote tree using that commit. A local checkout remains preferred, so the same
source is never ingested twice merely because a remote fallback is available.

The integrity gate treats the manifest as untrusted input until it has been
recomputed and compared field-for-field. It verifies content and normalized
document hashes, exact metadata and provenance, repository/source URL/path,
ref/commit, preflight and schema versions, raw/document hashes, Git tree/blob
SHA, retrieval/freshness timestamps, expiry, and chunk count. Each active chunk
is checked against the same source, trust, provenance, version, hash, index,
count, and freshness invariants. Any mismatch is stale/unknown and blocks
read-only preflight; `--fix` may replace it only after a fresh authoritative
fetch and verified ingestion.

## Consequences

Implementation may incur a bounded network lookup when a remote source is
missing or stale, but protected work receives a truthful fail-closed result.
The manifest makes provenance and drift reviewable, while the existing
document adapter remains available for general unverified local documents.
The complete Ruflo/RuVector inventory is runtime-generated from authoritative
commits rather than vendored into Verdict, avoiding stale duplicate copies.

## Completion and reconciliation

The enforcement implementation was merged by PR #154. Follow-up issue #156
reconciled source drift, completed active chunk validation, and provided the
machine-readable `doctor --json` and `memory docs --json` diagnostics. Issue
#158 closes the remaining audit gaps for remote Ruflo fallback and manifest
self-integrity verification. The shared-memory reconciliation receipt is
maintained on the ticket because the database and raw upstream payloads are
runtime state and are intentionally not committed.

The current inventory observed during reconciliation is 704 paths: Verdict
Core 7, Ruflo 284 at `26c35b59b40a0a95b286ccf5ac675a15edcc995f`, and RuVector
413 at resolved ref `597be6a753472f0521fe2def097116e717ed4332`. Six duplicate
blob projections remain visible in diagnostics and retrieval paths. After the
#158 verified repair, the shared plane contains 704 active manifests and 10,973
active authoritative chunks; prior source versions are retained only as
superseded history. A remote-only Ruflo verification resolved commit
`a158418a8b774f678dd36831be4ad1d5619b3395`, inventoried 286 ADR paths,
including nested/plugin projections, and validated all Git blob SHAs. Five
duplicate blob projection groups remain intentionally visible. The repaired
shared plane has 704 fresh manifests and passes the full integrity validator;
read-only preflight reports ready with zero stale, missing, unverifiable, or
orphaned records.
