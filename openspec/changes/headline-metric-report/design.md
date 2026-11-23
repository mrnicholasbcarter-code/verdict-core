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

## Decisions

**Decision 1: receipt-based paid share as the primary denominator.**
`paid_model_share` (subscription + metered attempts / total) is always
computable from local receipts without network access. OmniRoute gateway quota
(`with_quota`) is a secondary enrichment because most connections return
`quotaTotal: null`. Both are reported when available; neither is hidden.

**Decision 2: `completion_verdict` determines COMPLETE, not `claimed_outcome`.**
`extend_run_metrics` trusts `compute_run_metrics` / `completion_verdict` to
recompute outcome from evidence. A receipt whose `claimed_outcome` says BLOCKED
but whose nodes are all VALIDATED is treated as COMPLETE. This matches the
existing invariant and avoids a parallel gate.

**Decision 3: local imports moved to module level in `headline_metric.py`.**
Keeping `extend_run_metrics` as a local function import would hide it from the
reachability checker (`git ls-files` + AST scan). Moving to module level keeps
the production call chain visible without any runtime cost.

## Risks / Trade-offs

- `fetch_live_quota` makes an HTTP call to `localhost:20128`. The 5-second
  timeout and graceful fallback (`None` on any failure) mean a down gateway
  does not block the report; it just omits the quota section.
- The `test-` prefix convention for fixture exclusion is by agreement, not a
  signed property. Any live run whose `run_id` starts with `test-` is silently
  excluded. The report says how many were excluded so the operator sees if this
  fires unexpectedly.

## Security / trust implications

No new credentials, secrets or external writes. `fetch_live_quota` reads from
`localhost:20128` only (the local OmniRoute gateway); the response is treated as
untrusted data. No receipt contents are written or transmitted. The action
returns a plain dict; no credentials appear in the output.

## Routing / context / memory implications

No change to routing, admission, or eligibility logic. `extend_run_metrics`
reads an already-verified receipt dict and performs no I/O. `load_receipts_since`
calls the existing `verify_run_receipt` and reads the existing `receipt.json`
files; it never writes. No context pack or memory store is touched.

## Migration / rollback

Additive. All existing `verdict metrics` behaviour (per-run table, `--json`)
is unchanged. `--daily`, `--since`, and `--quota` route to the new action only
when explicitly passed; no existing call site is affected. Rollback is reverting
this branch: receipts already on disk are unaffected.

## ADR impact

None. The headline metric report is a read-only observability tool. No admission
gate, proof authority, or routing boundary changes. ADR-036 (goal-to-receipt),
ADR-049 (cost policy), and ADR-050 (Hindsight memory) are unaffected.
