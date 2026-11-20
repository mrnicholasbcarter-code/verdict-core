# Tasks

## 1. Receipt pack size per node

Owned files: `verdict/orchestration/receipt.py`. Verification:
`uv run python -m pytest -q tests/test_orch_receipt.py`.

- [x] 1.1 Read AGENTS.md and ADR-036 and existing receipt tests.
- [x] 1.2 Add `hydrate` event handling in `_node_record`; project `context_pack` dict
      onto the current attempt row.
- [x] 1.3 Update module docstring to document the `hydrate` event convention.
- [x] 1.4 Confirm digest binding is unchanged (verify_run_receipt is key-by-key, not
      hash-of-receipt).

## 2. `verdict bench context-lift` CLI and timer

Owned files: `verdict/context_lift_service.py` (new),
`verdict/commands/parsers_autodev.py`, `verdict/commands/dispatch.py`,
`verdict/setup_host.py`, `verdict/doctor_diagnostics.py`. Verification:
`uv run python -m pytest -q tests/test_bench_context_lift.py tests/test_doctor_host_prerequisites.py`.

- [x] 2.1 Add `verdict/context_lift_service.py` following the `acceptance_service` /
      `pr_watcher_service` pattern.
- [x] 2.2 Add `bench context-lift` to `parsers_autodev.py` with `--json`, `--base-url`,
      `--dry-run`, `--install-service`, `--uninstall`.
- [x] 2.3 Add dispatch routing in `dispatch.py`; write evidence to
      `$VERDICT_HOME/context-lift/`; exit 0 on blocked.
- [x] 2.4 Wire `CONTEXT_LIFT` into `setup_host.py` ORDER, `_problems_for` and
      `run_host_action`.
- [x] 2.5 Add `_context_lift_timer_row` to `doctor_diagnostics.py` and append to
      `installed_service_rows`.

## 3. Validate

Depends on groups 1–2. Verification: focused tests, ruff check/format, mypy --strict,
reachability ratchet, `check_lint_gates.py`, `openspec validate --strict`, vgate, PR CI.

- [x] 3.1 Run `tests/test_orch_receipt.py` and new bench/doctor tests.
- [x] 3.2 ruff check/format, mypy --strict, check_lint_gates.py.
- [x] 3.3 Reachability ratchet shows nothing new.
- [x] 3.4 `openspec validate --strict` passes.
- [x] 3.5 Push through vgate; open PR; CI green.
- [ ] (post-merge) 1.1 Timer fires and writes evidence under `$VERDICT_HOME/context-lift/`
      on the production host; `verdict doctor` shows context-lift timer ok.
