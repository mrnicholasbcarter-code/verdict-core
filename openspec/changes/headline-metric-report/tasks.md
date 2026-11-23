# Tasks: headline-metric-report

## 1. Core metric extension

Verification: `uv run python -m pytest -q tests/test_headline_metric.py` (35 tests pass).

- [x] 1.1 Add `extend_run_metrics` to `verdict/orchestration/receipt.py` with per-role splits,
  frontier tokens, first-attempt pass rate, and research-duplication note.
- [x] 1.2 `verdict/headline_metric.py` — new module: `load_receipts_since`,
  `aggregate_extended_metrics`, `fetch_live_quota`, `compute_headline_report`,
  `format_headline_report`.
- [x] 1.3 `verdict/actions/registry.py` — `_action_headline_metric` and `headline.metric`
  ActionSpec registration.

## 2. CLI extension

Verification: `uv run python -m pytest -q tests/test_headline_metric.py::TestActionHeadlineMetric`; also `uv run verdict metrics --daily` exits 0 on a fresh install with "0 runs processed".

- [x] 2.1 `verdict/orchestration/cli.py` — add `--daily`, `--since DATE`, `--quota` flags
  to `verdict metrics`.
- [x] 2.2 `_metrics` dispatch routes to `headline.metric` when `--daily`, `--since`,
  or `--quota` is set.

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

## 5. Daily timer (follow-on)

Verify: `verdict setup --apply --allow host.headline-metric-timer` exits 0; `verdict doctor` shows "Headline metric timer: ok"; the timer fires within 24 h and writes `~/.verdict/headline-metric-daily.json`.

- [ ] (post-merge) 5.1 Create `verdict/headline_metric_service.py` — systemd user timer
  that runs `verdict metrics --daily --json > ~/.verdict/headline-metric-daily.json`
  once per day (pattern: `discovery_refresh_service.py`).
- [ ] (post-merge) 5.2 Wire into `verdict/setup_host.py` as `host.headline-metric-timer`.
- [ ] (post-merge) 5.3 Wire doctor row into `verdict/doctor_diagnostics.py` (small addition).
- [ ] (post-merge) 5.4 `verdict setup --apply --allow host.headline-metric-timer` and
  confirm the timer fires; record raw output.
- [ ] (post-merge) 5.5 `verdict doctor` shows "Headline metric timer: ok".

## 6. M0 baseline

- [ ] (post-merge) 6.1 Run `verdict metrics --daily --json` on operator's machine against
  real receipts; save output as M0 baseline with date and git SHA.
  Record in `docs/proof/EVIDENCE_INDEX.md`.
