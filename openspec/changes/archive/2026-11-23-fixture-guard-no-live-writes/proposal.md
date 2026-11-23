# Proposal: fixture-guard-no-live-writes

**Story L1a** (learning-loop plan, 2026-11-20).

## Problem

Fixture and demo runs write rows into the user's live state stores:

1. `scripts/demo_orchestrate.py` calls `run_golden_path` without first
   redirecting state stores. The runtime's `SessionLedger().append(...)` and
   `EligibilityLadder.record_success()` → `record_dispatch_observation()` both
   fall through to their VERDICT_HOME defaults, writing fixture route rows
   (`alpha/claude-a`, `demo-sub/atlas-coder`, etc.) into
   `~/.verdict/session-evidence.jsonl` and `~/.verdict/prove-at-rest/state.json`.
   This is confirmed by the learning-loop plan (2026-11-20): 26 fixture rows
   were found in the live ledger and manually purged.

2. `scripts/prime_supervisor.py:_build_intelligence_service_from_config` builds
   `passport_store` from `Path.home() / ".verdict" / "prove-at-rest" / "state.json"`,
   ignoring `VERDICT_HOME`. When `VERDICT_HOME` points elsewhere, the supervisor
   reads the wrong passport state (Lane C item: `test-writes-live-passport-store`).

3. `VERDICT_SESSION_EVIDENCE` was missing from `_HOST_STATE_ENV` in
   `tests/conftest.py`. An operator-exported value would leak into tests and
   direct `SessionLedger()` appends to the real file.

4. Several `Path.home() / ".verdict"` calls in `scripts/` bypass `verdict_home()`,
   violating the VERDICT_HOME contract the package enforces.

## Fix

- `scripts/demo_orchestrate.py`: add `scoped_state_env(root / "fixture-state")`
  around the `run_golden_path` call (same pattern as `demo_scenario.py`).
- `scripts/prime_supervisor.py`: replace all `Path.home() / ".verdict" / ...`
  with `verdict_home() / ...` (four sites: `ladder_state_path`, the prime-target-map
  fallback, `passport_store`, and `metadata_store`).
- `scripts/ingest_dependency_docs.py`: replace `Path.home() / ".verdict" / "memory.db"`
  with `verdict_home() / "memory.db"`.
- `tests/conftest.py`: add `VERDICT_SESSION_EVIDENCE` to `_HOST_STATE_ENV`.
- `tests/test_first_run_state_isolation.py`: extend the path-scan to `scripts/`;
  add three acceptance tests (byte-identical demo run, prime_supervisor passport
  path, session-evidence isolation).
- Use an OpenSpec change because `demo_orchestrate.py` and `prime_supervisor.py`
  behaviour changes (state-store resolution).
