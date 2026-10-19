# Design

## Context

Waves 3-5 of V1 hardening: S03 (BOD-433), S06 (BOD-434), S05 phase 1 (BOD-297) and S07 (BOD-435). Wave 2 is in `openspec/changes/v1-authority-inventory`.

## Decisions

1. **Reduced dispatch scope**: `DispatchScope` binds gateway origin, `config_generation` (a SHA-256 over the selected provider, model and route id), the task profile digest and a `binding_id`. It holds no key material. Full effective-config generation is a follow-up. See the ADR-035 addendum "Dispatch scope binding (reduced)".
2. **Permit linearization**: `commit_dispatch` runs under the health cache state lock. It denies when the proof's captured epoch differs from the current scope epoch, when the attempt id was already used, or when the proof is expired or stamped in the future. A minted permit is stored as `maybe_sent` and is never released. A retry needs a new attempt id and a new permit.
3. **Revocation**: `record_revocation` is idempotent per (scope, event id) and increments a persisted per-scope epoch. Auth, permission, model-not-found (structured error code only) and identity-mismatch failures revoke the scope. `admission_proof` denies a passport whose `qualified_at` is not strictly after `latest_revocation_at`.
4. **Persistence**: the `dispatch` envelope is written only when non-empty. Merges take the union with disk winning and the epoch maximum, so a stale writer cannot drop a permit or lower an epoch.
5. **Relay binding**: the relay accepts exactly one proof whose model and route id match the attempt, recomputes the profile digest with `prepared_task_profile`, and denies on any scope mismatch with zero sends.
6. **Worker binding**: `WorkerController` in authority mode (set by `cli_run` through `gateway_origin`) binds a proof and commits a permit immediately before `adapter.spawn`. No proof or a denied commit means zero spawns.
7. **DagRuntime not wired**: `RuntimePolicy.require_dispatch_permit` defaults to `False`. When set it denies before execution. The production caller in `verdict/orchestration/run.py` does not set it.
8. **Stream terminal**: `SSETerminalParser` frames SSE independent of chunking and tracks chat (`finish_reason`, optional usage, `[DONE]`) and Responses (`response.completed`, `response.failed`, `response.incomplete`) terminals. Anything else fails closed. `ValidatedSSEStream` passes original bytes through, records one `StreamOutcome`, and closes upstream once through `CloseOnce`. Upstream transport errors record `transport_error`.
9. **Scope-bound refresh**: `RefreshSnapshot.scope` is separate from the evidence `generation`. A snapshot with no explicit scope derives a `gateway:<sha>` scope. Debounce and join require scope equality. A scope-less legacy marker matches only a gateway-derived scope. `max_per_provider` caps probes per normalized provider.
10. **Public models**: a lifespan task refreshes a `PublicModelsReadiness` snapshot every 20 s with a 5 s load timeout. Source snapshots and basic proofs must be younger than 600 s. Incomplete, absent, corrupt or expired inventory returns a classified 503. A valid empty inventory returns 200 with an empty list. Last-good data never outlives its original deadline.

## Risks / Trade-offs

- Phase 2c is open: `DagRuntime` runs without permits in production.
- The relay recomputes the profile digest with its own planner; it must equal the planner used at route time.
- Alternatives carry no per-alternative provider on the routing decision; they are bound by unique model and route id.
- Nothing in this change writes the source snapshots on a cadence under 600 s; `GET /v1/models` returns 503 `inventory_expired` until a producer keeps them fresh.
- `GET /v1/models` now lists verified models only; clients that relied on the full upstream catalog see less.
- The maintenance refresh lane, crash ledger, concurrency lanes, renewal slots, durable backlog cursor and producer rewiring (S05 later phases) are not delivered. `output_tokens` and `cooldown_seconds` are validated config knobs without behaviour yet.
