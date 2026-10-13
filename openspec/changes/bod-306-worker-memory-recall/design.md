# Design: Worker memory recall (BOD-306 slice 1)

## Context

`build_worker_context` already runs `plan_context_query` and `execute_context_query` with native providers wrapped by `_SafeNativeProvider`. Then `_cited` re-reads and re-cites each unit, and `_cited` dropped every `memory` and `history` unit. `execute_context_query` already accepts a `MemoryPlane`, and `NativeMemoryProvider` already serves `memory.search` from it. `MemoryGate` is the only writer that sets `authority_verified=True` after checking its authority registry and namespace policy.

## Goals / Non-Goals

Goal: a worker sees gated facts relevant to its node, with citations, inside the existing budget. Non-goals: transcript or session recall, shared-provider recall, new namespaces or writers, ranking changes and receipt schema changes.

## Decisions

1. Reuse `NativeMemoryProvider` through the existing query path. Do not add a new provider. Its new hints are optional, and existing callers are unaffected.
2. Recall only `authority_verified` records in `decisions`, `feedback`, `patterns` and `evidence`. `session_history`, `files`, `sessions`, `authoritative-docs` and gate-event namespaces are excluded. This follows the bod-303 design: facts only, written through MemoryGate.
3. Allow `public`, `standard` and `internal` sensitivity. Workers already receive internal repository source. `confidential`, `restricted` and unknown labels never enter a prompt.
4. Query with the distinct content words of the node (objective, acceptance, symbols), at most 12. MemoryPlane ORs the terms and ranks them with BM25.
5. `memory.search` is the last capability, and the pack orders `required:` units first, so memory is cut first under budget pressure. At most two units are recalled.
6. A memory unit is cited like a file excerpt, with `source=memory:<record_id>` and `span=null`. It is redacted with `redact_text` before the digest is taken.
7. Open `shared_memory_path()` (`VERDICT_MEMORY_PLANE_PATH` or `~/.verdict/memory.db`) with the new `MemoryPlane.open_readonly`, and close it after the query. It opens an immutable snapshot (SQLite `mode=ro&immutable=1`, `query_only`, schema version checked), so no file is touched, not even the `-wal`/`-shm` sidecars. Such a snapshot cannot see committed WAL frames and is not protected from a concurrent writer. So the database and WAL identity, size and change times are recorded before the open, re-checked after it, and checked again after the query. A non-empty WAL or any change between those points drops the recalled units and is reported as `memory.search:memory_plane_busy`, never a stale or torn view. If the file is missing, the provider reports `no_default_location`. A file that is not a current MemoryPlane produces `memory.search:memory_plane_unavailable` and is left unchanged. The `MemoryPlane(path)` constructor creates and migrates, so a reader must not use it.

## Risks / Trade-offs

A lexical OR query can recall a weakly related fact. Two units and the budget cap the cost. MemoryPlane FTS over a large plane takes about 20-300 ms per query on the operator's 157 MB plane, measured on a copy. While a writer holds uncheckpointed WAL frames, recall is skipped (`memory_plane_busy`). SQLite checkpoints on writer close, and the operator's plane has an empty WAL today. A gated fact can still be wrong. It is labelled advisory evidence, never authority.

## Security / trust implications

Only records the gate verified are rendered. MemoryGate stores caller-supplied `trust`, `source`, tags and provenance as given, so every rendered field (content, trust, authority, revision) is redacted. The whole rendered citation is then checked once more, and the unit is dropped if anything still matches a credential shape. Key and source URIs that look like secrets are dropped. Content stays quoted evidence under the existing injection sanitizer. No exception text is rendered.

## Routing / context / memory implications

No routing or admission change. The pack reads memory and never writes it. MemoryGate, the outbox and shared recall are unchanged. Receipts already record pack sources through `WorkerPrompt.evidence()`, so the recalled record ids appear there.

## Migration / rollback

No migration. Removing `memory.search` from `_CAPABILITIES` restores the previous packs.

## ADR impact

Applies ADR-004 (local MemoryPlane is the authority) and ADR-009 (MemoryGate verifies writes) on the ADR-036 worker path. This change supersedes decision 5 of `bod-303-worker-context-packs` as that design anticipated. No new ADR is needed.
