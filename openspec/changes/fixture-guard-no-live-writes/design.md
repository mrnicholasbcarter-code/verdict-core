# Design: fixture-guard-no-live-writes

## Approach

**Fix at the writer, not at the reader.** Every call site that writes to a
state store must route through `verdict_home()` or through `scoped_state_env()`.
No test-only special cases; the same code path that tests exercise is the one
production uses.

### demo_orchestrate.py

`run_demo` already creates `root = Path(tempfile.gettempdir()) / "verdict-demo"`.
Wrap the entire run body with `scoped_state_env(root / "fixture-state")` saved
and restored via `os.environ`, mirroring `demo_scenario.py:run_flagship_scenario`.
This sets `VERDICT_HOME`, `VERDICT_SESSION_EVIDENCE`, `VERDICT_HEALTH_CACHE`, and
`VERDICT_PROVE_AT_REST_STATE` to paths inside `root/fixture-state` for the
duration of the run.

### prime_supervisor.py

Replace each `Path.home() / ".verdict"` reference with `verdict_home()`. Import
`verdict_home` as a lazy local import inside the affected functions (consistent
with the file's existing pattern of lazy verdict imports).

Affected sites:
- `ladder_state_path()` → `verdict_home(env) / "orchestration-health.json"`
- `_load_prime_target_map()` fallback → `verdict_home() / "prime-target-map.json"`
- `_build_intelligence_service_from_config()` passport_store and metadata_store

### ingest_dependency_docs.py

The `--memory-path` default uses `Path.home() / ".verdict" / "memory.db"`.
Replace with `verdict_home() / "memory.db"`. Since `verdict_home()` is evaluated
at arg-parse time (Python evaluates `default=` at function-definition time), the
behaviour is identical to the existing code except it now reads `VERDICT_HOME`.

### conftest.py

Add `"VERDICT_SESSION_EVIDENCE"` to `_HOST_STATE_ENV`. The autouse
`_isolate_host_state_env` fixture already drops all other state-store overrides;
this closes the final gap. Per-test `monkeypatch.setenv("VERDICT_SESSION_EVIDENCE",
...)` calls still work because autouse `delenv` runs before each test, and
individual test setenv runs after.

## Invariants preserved

- `verdict_home()` already handles empty-string VERDICT_HOME and tilde expansion.
- `scoped_state_env()` already sets all four store overrides; demo_orchestrate
  gains the same contract `demo_scenario.py` already satisfies.
- No production caller is changed; only the scripts that set up the environment.
