# Proposal

## Why

Waves 3-5 of the V1 hardening plan make dispatch revocable, make streamed responses end in a checked terminal state, give every verified-refresh producer one stable scope, and publish only locally verified models. The complete 13-story umbrella plan is in `docs/contracts/v1-hardening-umbrella-plan.md` and tracked in Linear under BOD-431. Wave 2 (S02, S04) is in `openspec/changes/v1-authority-inventory`. This change covers S03 (BOD-433), S06 (BOD-434), S05 phase 1 (BOD-297) and S07 (BOD-435).

## What Changes

- **Dispatch permits (S03 / BOD-433)**: `verdict/orchestration/health_cache.py` gains `DispatchScope`, `DispatchPermit`, `HealthCache.commit_dispatch`, `record_revocation`, `revocation_epoch` and `latest_revocation_at`. The API relay (`verdict/api.py`, `verdict/relay.py`) commits a durable permit before each send. The worker spawn path (`verdict/worker_runtime.py`, `verdict/admit_prove_confirm.py`) commits a permit before each spawn. `RuntimePolicy.require_dispatch_permit` exists but defaults to off (phase 2c, `DagRuntime`, is not delivered).
- **Stream terminal semantics (S06 / BOD-434)**: new `verdict/stream_terminal.py` replaces the SSE validator in `verdict/api.py`. It checks chat and Responses terminal events, bounds buffers, and closes upstream exactly once.
- **Shared qualification, phase 1 (S05 / BOD-297)**: `RefreshCoordinator` in `verdict/orchestration/verified_refresh.py` binds reuse to a stable scope, joins and debounces per scope, and caps probes per provider. The maintenance lane is deferred.
- **Public verified models (S07 / BOD-435)**: `GET /v1/models` serves a locally verified projection from a lifespan-owned refresh task instead of proxying the upstream catalog.
- **Test infrastructure**: the proof runner runs the full suite once, and the test conftest isolates host state paths and `systemctl` writes. No production behaviour changes.
- **ADR-035** has an addendum, "Dispatch scope binding (reduced)".

## Capabilities

### New Capabilities
- `dispatch-permits`: durable per-attempt permits and per-scope revocation epochs gating every send and spawn.
- `stream-terminal`: checked SSE terminal outcomes with bounded buffers and close-once.
- `shared-qualification`: scope-bound refresh reuse and per-provider fairness.
- `public-verified-models`: `/v1/models` as a local verified projection.

### Modified Capabilities

## Impact

Affects `verdict/orchestration/health_cache.py`, `verdict/orchestration/runtime.py`, `verdict/orchestration/verified_refresh.py`, `verdict/orchestration/verified_models.py`, `verdict/actions/verified_models.py`, `verdict/api.py`, `verdict/relay.py`, `verdict/intelligence.py`, `verdict/worker_runtime.py`, `verdict/admit_prove_confirm.py`, `verdict/stream_terminal.py`, `scripts/proof/runner.py`, `tests/conftest.py`, `docs/adr/ADR-035-authorized-selected-route-dispatch.md`. No new dependency. Pre-merge implementation and automated tests only; live acceptance on merged main remains with the operator.
