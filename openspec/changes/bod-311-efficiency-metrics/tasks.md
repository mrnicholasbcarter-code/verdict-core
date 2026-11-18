# Tasks

## 1. Core metrics computation and action

Owned files: `verdict/orchestration/receipt.py` (add `compute_run_metrics`), `verdict/actions/registry.py` (add `_action_metrics`). Verification: focused tests in `tests/test_orch_metrics.py`, source ruff check/format, strict mypy on changed source.

- [x] 1.1 Add `compute_run_metrics(receipt: Mapping[str, Any]) -> dict[str, Any]` in `receipt.py` that returns the four numbers plus raw counts. (Reticked after review: accepted tasks now require `completion_verdict` COMPLETE; raw attempt counts returned and summed.)
- [x] 1.2 Add `_action_metrics` in `registry.py` that scans runs, verifies receipts, aggregates.
- [x] 1.3 Handle tampered receipts (verify_run_receipt returns problems) — skip with warning.
- [x] 1.4 Handle unknown costs — report as unknown, never 0.
- [x] 1.5 `build_run_receipt` takes `run_id` from the digest-bound `run_started` event before `graph.json`, so an untampered run whose graph keeps a planner-input id verifies; editing any receipt field still fails (`tests/test_orch_receipt.py`).
- [x] 1.6 Tests: CANCELLED -> 0, failed OpenSpec conformance -> 0, edited BLOCKED -> 0, COMPLETE -> n, raw-count aggregation (`tests/test_orch_metrics.py`).

## 2. CLI command

Owned files: `verdict/orchestration/cli.py` (add `metrics` subparser and `_metrics` dispatch). Verification: same focused tests, CLI integration test.

- [x] 2.1 Add `metrics` subparser: `path` (run dir or runs dir), `--runs-dir`, `--json`.
- [x] 2.2 Implement `_metrics` dispatch function calling `run_action("metrics", ...)`.
- [x] 2.3 Text and JSON output formatting.

## 3. Fixture receipts and tests

Owned files: `tests/fixtures/metrics/` (fixture runs), `tests/test_orch_metrics.py`. Verification: focused tests pass, fixtures labelled as fixtures.

- [x] 3.1 Create fixture run directories with receipts:
    - `complete-run/` — COMPLETE, mixed capacity, known costs.
    - `tampered-run/` — receipt digest mismatch.
    - `unknown-cost-run/` — COMPLETE, some costs null/missing.
    - `blocked-run/` — BLOCKED (not counted).
- [x] 3.2 Write `tests/test_orch_metrics.py` covering:
    - Single run metrics computation.
    - Directory aggregation across multiple runs.
    - Tampered receipt refused.
    - Unknown cost reported correctly.
    - Free/subscription share calculation.
    - JSON output format.

## 4. Real-run validation

Owned files: this OpenSpec change, evidence pasted in PR. Test verification: manual run on BOD-300 and recent runs; output captured and pasted in PR.

- [x] 4.1 Run `verdict metrics` on `/home/nick/dev/verdict-core/.git/verdict-prime/bod300-confirm/runs/`.
- [x] 4.2 Run on any recent run dirs found under `/home/nick` and `/tmp`.
- [x] 4.3 Paste output in PR description.

## 5. Quality gates

Owned files: all changed source. Test verification: automated CI checks (ruff, mypy, doc links, reachability, OpenSpec validators).

- [x] 5.1 `uv run ruff check . && uv run ruff format --check .`
- [x] 5.2 `uv run mypy verdict --strict`
- [x] 5.3 `uv run python scripts/check_doc_links.py`
- [x] 5.4 `uv run python scripts/check_reachability.py --ratchet-base origin/main`
- [x] 5.5 `uv run python scripts/check_openspec_tasks.py --base origin/main`
- [x] 5.6 Both OpenSpec validators pass (reticked after `.openspec.yaml` got `schema: verdict-change-v1` and the `efficiency-metrics` delta).

## 6. PR and merge

Test verification: git push, vgate, gh pr create --draft with real-run output; CI validation and human review.

- [x] 6.1 Push branch, `vgate` passes.
- [x] 6.2 `gh pr create --draft` with real-run output pasted.
- [x] 6.3 Independent re-review from a different model family of the fixes for the first review (cc/claude-sonnet-5, BLOCKING). Re-review at c89e6b26 by kilocode/nvidia/nemotron-3-ultra-550b-a55b:free (pick-reviewer, nvidia family): pass; orchestrator re-ran run-receipt on clean, receipt-edited and events-edited copies (exit 0, 1, 1).
- [ ] (post-merge) 6.4 Squash-merge pinned to the head SHA and rerun `verdict metrics` on clean main.