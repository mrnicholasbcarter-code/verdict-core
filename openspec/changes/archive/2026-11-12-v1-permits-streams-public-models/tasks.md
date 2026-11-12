# Tasks

## 1. Waves 3-5 — permits, streams, qualification phase 1, public models

- [x] 1.1 **S03 / BOD-433 phase 1** (core): durable dispatch permits and per-scope revocation epochs in `verdict/orchestration/health_cache.py`. Proof: `pytest -q tests/test_health_cache.py tests/test_dispatch_permits.py`.
- [x] 1.2 **S03 / BOD-433 phase 2** (API relay): relay commits a permit before every send and denies stale, foreign or revoked proofs. Proof: `pytest -q tests/test_api_dispatch_permits.py tests/test_relay_admission.py`.
- [x] 1.3 **S03 / BOD-433 phase 2b** (worker spawn): worker spawn commits a permit before `adapter.spawn`. Proof: `pytest -q tests/test_worker_dispatch_permits.py`.
- [x] 1.4 **S06 / BOD-434** (stream terminal semantics, wired into the relay). Proof: `pytest -q tests/test_stream_terminal.py tests/test_stream_relay_wiring.py`.
- [x] 1.5 **S05 / BOD-297 phase 1** (stable scope binding, join and debounce, per-provider normalization). Proof: `pytest -q tests/test_verified_model_refresh.py tests/test_verified_refresh_consumers.py`.
- [x] 1.6 **S07 / BOD-435** (public verified models and lifespan refresh). Proof: `pytest -q tests/test_relay_admission.py tests/test_proxy.py tests/test_verified_models_projection.py`.
- [x] 1.7 **Test infrastructure**: the proof runner runs the full suite once, and the test conftest isolates host state paths and `systemctl` writes. Proof: `pytest -q tests/test_proof_pipeline.py`.
- [x] 1.8 **ADR-035** addendum "Dispatch scope binding (reduced)". Proof: `grep -n "Dispatch scope binding (reduced)" docs/adr/ADR-035-authorized-selected-route-dispatch.md`.

## Scope and Delivery Notice

- **Active scope**: Waves 3-5, pre-merge implementation and automated test verification only.
- **Not claimed**: live acceptance on the operator's gateway from merged main, full DoD proof, Done status, or archive.
- **Not delivered**: phase 2c (`DagRuntime` permits; `RuntimePolicy.require_dispatch_permit` defaults to off) and the S05 maintenance lane and later phases.
- **Umbrella plan**: `docs/contracts/v1-hardening-umbrella-plan.md` is unchanged.
