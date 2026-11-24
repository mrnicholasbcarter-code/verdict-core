# Tasks: headline-metric-report

## 1. Core metric extension

Verification: `uv run python -m pytest -q tests/test_headline_metric.py`.

- [x] 1.1 Add `extend_run_metrics` to `verdict/orchestration/receipt.py` with per-role splits,
  frontier tokens, first-attempt pass rate, and research-duplication note.
- [x] 1.2 `verdict/headline_metric.py` — new module: `load_receipts_since`,
  `aggregate_extended_metrics`, `compute_headline_report`,
  `format_headline_report`.
- [x] 1.3 `verdict/actions/registry.py` — `_action_headline_metric` and `headline.metric`
  ActionSpec registration.

## 2. CLI extension

Verification: `uv run python -m pytest -q tests/test_headline_metric.py::TestActionHeadlineMetric`; also `uv run verdict metrics --daily` exits 0 on a fresh install, printing `Runs processed:   0` and `Runs directory not found: <dir>` (#1315 made this true: the merged action returned exit 2).

- [x] 2.1 `verdict/orchestration/cli.py` — add `--daily` and `--since DATE` flags
  to `verdict metrics`.
- [x] 2.2 `_metrics` dispatch routes to `headline.metric` when `--daily` or `--since`
  is set.

## 3. Tests

- [x] 3.1 `tests/test_headline_metric.py` — unit tests for `extend_run_metrics`,
  fixture exclusion, aggregate, action, and CLI flags.
- [x] 3.2 Fixture test: verify fixture runs (test-* run_id) are excluded from
  the live aggregate.

## 4. Quality gates

- [x] 4.1 `uv run --frozen ruff check . && uv run --frozen ruff format --check .`
- [x] 4.2 `uv run --frozen mypy verdict --strict`
- [x] 4.3 `uv run --frozen python scripts/check_lint_gates.py`
- [x] 4.4 Focused tests pass: `uv run python -m pytest -q tests/test_headline_metric.py`
- [x] 4.5 Full suite passes (n=4): `uv run python -m pytest -q -n 4`

## 5. M0 baseline

- [ ] (post-merge) 6.1 Run `verdict metrics --daily --json` on operator's machine against
  real receipts; save output as M0 baseline with date and git SHA.
  Record in `docs/proof/EVIDENCE_INDEX.md`.

## 7. Follow-up #1315 (post-merge review of #1298)

Verification: `uv run python -m pytest -q tests/test_headline_metric.py tests/test_orch_metrics.py`.

- [x] 7.1 Trace finding 1 (a first attempt that returns ok, then fails validation):
  the runtime emits a `failure` event for the same attempt after `_validate`
  rejects it, so the attempt row reads `failure`. Not a defect; pinned by
  `TestFirstAttemptCountsOnlyValidatedWork::test_worker_success_then_failed_validation_is_not_a_pass`
  (real `DagRuntime`, real `EventLog`).
- [x] 7.2 First-attempt pass requires a `VALIDATED` node: the traced narrower
  case (worker ok, validation never finishes) was counted as a pass
  (`verdict/orchestration/receipt.py`, `extend_run_metrics`).
- [x] 7.3 `verdict metrics <path>` reads `<path>` (`verdict/orchestration/cli.py`,
  `_metrics`); a path that resolves to no directory exits 2.
- [x] 7.4 A missing runs directory is an empty report with exit 0
  (`verdict/actions/registry.py`, `verdict/headline_metric.py`); the report
  carries `runs_dir` and `runs_dir_found`. The old
  `test_action_missing_runs_dir` (which pinned exit 2) is replaced.
- [x] 7.5 Verified bytes are aggregated bytes: `load_receipts_since` reads once
  and calls `verify_run_receipt_from_bytes`.
- [x] 7.6 Tests written first and shown failing against the untouched
  production code, then passing (`tests/test_headline_metric.py`).
- [x] 7.7 Gates pass on the branch: `ruff check`, `ruff format --check`,
  `mypy verdict --strict`, `scripts/check_lint_gates.py` (OpenSpec validate and
  open-task check included), `git diff --check`.
