# Design: M0 headline metric report

## Context

BOD-311 delivered `compute_run_metrics` and `verdict metrics`. Issue #1213
requires the M0 headline metric: accepted work per % paid quota, with per-role
splits, frontier tokens, first-attempt pass rates, and a daily report command.

## Goals / Non-Goals

**Goals:**
- Add per-role splits and frontier-token counts to the metrics surface.
- Add first-attempt pass rate by role and by cost class.
- Compute "accepted tasks / paid model share" from receipts (always available).
- Enrich with OmniRoute quota data when the gateway is reachable (optional).
- Exclude fixture runs from the live aggregate; say so explicitly.

**Non-Goals:**
- Planner/reviewer individual call token tracking (not in receipts; task filed).
- Research-duplication share (not in receipts; `research_metrics.py` has the
  per-call tool; a task is filed to wire it in).
- Daily timer service (follow-on task; deliver the command first).

## Denominator

"% of paid quota" can mean two things:
1. The fraction of model attempts on paid routes (from receipts).
2. The fraction of a provider's subscription window consumed (from OmniRoute).

Both are computed. Source 1 is always available. Source 2 uses
`check_quota` from the OmniRoute MCP / HTTP API; most connections return
`quotaTotal: null`, so the note explains this explicitly when unavailable.

## Fixture exclusion

Fixture runs have `run_id` starting with `test-`. `load_receipts_since` skips
them and reports `fixture_runs_excluded` in the aggregate, so the live report
never incorporates fixture data.

## Code Structure

### `verdict/orchestration/receipt.py` — `extend_run_metrics`

Calls `compute_run_metrics` for the base; adds:
- `attempts_by_role`: `{NodeKind.value: attempt_count}` over DAG nodes.
- `accepted_nodes_by_role`: accepted count per role (non-zero only for
  implement/integrate when COMPLETE).
- `attempts_by_cost_and_role`: nested `{role: {cost_class: count}}`.
- `frontier_tokens`: input/output/total tokens on subscription+metered attempts;
  `per_accepted_task` when at least one accepted task exists.
- `planner_attempts_count`: length of `receipt["planner_attempts"]` (call
  frequency; individual token counts not recorded in receipt).
- `reviewer_token_budget_used`: from `receipt["review"]["token_budget_used"]`.
- `first_attempt_pass_rate`: `{by_role: {role: float|None}, by_cost: {cost: float|None}}`.
- Notes for planner tokens and research duplication.

### `verdict/headline_metric.py`

- `load_receipts_since(runs_dir, since)`: loads, verifies, and filters.
- `aggregate_extended_metrics(receipts)`: aggregates `extend_run_metrics` output.
- `fetch_live_quota()`: HTTP GET `http://localhost:20128/api/quota`; returns
  None if unreachable.
- `compute_headline_report(runs_dir, since, with_quota)`: top-level entry point.
- `format_headline_report(data)`: terminal output.

### `verdict/actions/registry.py` — `_action_headline_metric`

Thin wrapper around `compute_headline_report`; registered as `headline.metric`.

### `verdict/orchestration/cli.py` — `_metrics` extension

`--daily`, `--since DATE`, `--quota` flags route to the headline action instead
of the per-run table when any of these flags is set.

## Test Strategy

- `test_extend_run_metrics_*`: in-memory receipt dicts; no fixture files needed.
- `test_fixture_runs_excluded`: verifies `load_receipts_since` skips `test-*`.
- `test_aggregate_extended_metrics_empty`: empty input.
- `test_compute_headline_report_*`: monkeypatches `load_receipts_since`.
- `test_action_headline_metric`: uses existing `tests/fixtures/metrics/`.
- No live calls in tests.
