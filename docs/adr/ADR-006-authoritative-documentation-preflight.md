# ADR-006: Authoritative documentation preflight before implementation

- **Status:** Accepted — implementation in progress on issue #152
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

## Consequences

Implementation may incur a bounded network lookup when a remote source is
missing or stale, but protected work receives a truthful fail-closed result.
The manifest makes provenance and drift reviewable, while the existing
document adapter remains available for general unverified local documents.
The complete Ruflo/RuVector inventory is runtime-generated from authoritative
commits rather than vendored into Verdict, avoiding stale duplicate copies.
