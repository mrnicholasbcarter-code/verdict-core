# ADR-017: Durable privacy-safe receipt ledger

## Status

Accepted for issue #117.

## Decision

Use a local SQLite ledger with WAL mode as the canonical persistence boundary
for decision and lifecycle receipts. Each append receives a stable ID, a
canonical payload hash, a metadata-bound record hash, a per-scope sequence, and
the previous record hash. Lifecycle changes and retention are new linked rows;
observed rows are not mutated.

The API’s explain compatibility surface uses the durable adapter when
`VERDICT_RECEIPTS_DB` is configured. Authenticated API startup fails closed when
that path is absent. Anonymous/test mode may opt into an explicit in-memory
backend and must not be presented as restart-safe audit storage.

Reads, exports, replay, and durable explain lookups verify the scoped chain and
fail closed on tampering. Payloads are recursively redacted by default, with
raw prompt/output/tool fields requiring exact field-level allowlists. The store
keeps key references only; encryption providers and credential vaults remain
outside its responsibility.

## Consequences

- Process restarts and concurrent workers share one durable evidence authority.
- Duplicate terminal callbacks are idempotent; conflicting terminal outcomes
  do not overwrite the accepted outcome.
- Tombstones provide logical retention/deletion without rewriting observed
  facts, while operators remain responsible for WAL, backup, and disk-retention
  cleanup.
- Generic receipt callers retain their existing `put_receipt`, query, and
  manifest interfaces; strict API evidence uses mandatory scope boundaries.
