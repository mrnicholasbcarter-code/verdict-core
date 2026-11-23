# Tasks — repo gate resolution uses the target repo's own environment

## 1. Implementation

- [x] 1.1 `verdict/orchestration/verification.py` — update `resolve_gate_argv`
  to insert a `uv run --frozen` step between `.venv` and Verdict-env checks,
  gated on `uv.lock` presence; raise `FileNotFoundError` when `uv.lock` is
  present but `uv` is not on PATH.
- [x] 1.2 `tests/test_repo_gates_runtime.py` — add 6 new tests covering the
  uv.lock resolution path, the env-failure path, and the unchanged non-uv
  fallback.
- [x] 1.3 Run focused tests: `pytest tests/test_repo_gates_runtime.py
  tests/test_repo_gates.py` — 65 passed.
- [x] 1.4 Run `uv run ruff check . && uv run ruff format --check . && uv run
  mypy verdict --strict && uv run python scripts/check_lint_gates.py` — all
  pass.
- [ ] (post-merge) 1.5 CI exact-head checks green; squash-merge to main.

## 2. Verification

- [x] 2.1 Verify: `test_resolve_uses_uv_run_when_uv_lock_present` — PASS.
- [x] 2.2 Verify: `test_resolve_raises_when_uv_lock_present_but_uv_missing` — PASS.
- [x] 2.3 Verify: `test_resolve_uv_lock_takes_precedence_over_verdict_env` — PASS.
- [x] 2.4 Verify: `test_resolve_no_uv_lock_still_uses_verdict_env` — PASS.
- [x] 2.5 Verify: `test_resolve_uses_uv_run_for_ruff_when_uv_lock_present` — PASS.
- [x] 2.6 Verify: `test_gate_uses_uv_run_when_worktree_has_uv_lock` — PASS.
- [ ] (post-merge) 2.7 On installed main, run `verdict orchestrate` against a
  verdict-core task and confirm `repo_gate` events show `executed_command`
  starting with the `uv` binary path with `run --frozen`, and `exit_code: 0`
  on a clean node.
