# Tasks

## 1. Implementation

- [x] 1.1 Add `scoped_state_env()` around `run_golden_path` in
      `scripts/demo_orchestrate.py`
- [x] 1.2 Replace `Path.home() / ".verdict" / ...` with `verdict_home() / ...` in
      `scripts/prime_supervisor.py` (four sites)
- [x] 1.3 Replace `Path.home() / ".verdict" / "memory.db"` with
      `verdict_home() / "memory.db"` in `scripts/ingest_dependency_docs.py`
- [x] 1.4 Add `"VERDICT_SESSION_EVIDENCE"` to `_HOST_STATE_ENV` in
      `tests/conftest.py`
- [x] 1.5 Extend `test_no_module_builds_state_path_from_path_home` to scan
      `scripts/` as well as `verdict/`
- [x] 1.6 Add `test_demo_orchestrate_leaves_session_evidence_and_passport_store_unchanged`
- [x] 1.7 Add `test_prime_supervisor_passport_store_follows_verdict_home`
- [x] 1.8 Add `test_session_evidence_env_var_isolated_in_conftest`
- [x] 1.9 Run focused tests, ruff, mypy --strict, `scripts/check_lint_gates.py`
- [x] 1.10 Push with vgate
- [x] (post-merge) 1.11 CI exact-head checks green; squash-merge to main
  Done 2026-11-23T19:08Z on 59957ae6: ci-ok and osv-security passed (runs 44827645266, 44827645479); squash-merged as PR #1238.

## 2. Live checks

- [x] (post-merge) 2.1 From installed merged main, run `python scripts/demo_orchestrate.py --out /tmp/demo-check` and confirm `~/.verdict/session-evidence.jsonl` and `~/.verdict/prove-at-rest/state.json` are unchanged (byte-compare before/after)
  Done 2026-11-23T19:05Z on ebc11cc8 (installed-main): SHA256 of session-evidence.jsonl and prove-at-rest/state.json identical before/after; "ISOLATION OK: live stores byte-identical" in first-run output (docs/proof/dod/bod-1215/l1a-first-20261123T190530Z.txt).
- [x] (post-merge) 2.2 Confirm `verdict demo` with a fresh VERDICT_HOME leaves the pre-run evidence and passport files unchanged
  Done 2026-11-23T19:05Z on ebc11cc8 (installed-main): same ISOLATION OK check as 2.1 — real ~/.verdict stores byte-identical before and after demo_orchestrate.py run.
