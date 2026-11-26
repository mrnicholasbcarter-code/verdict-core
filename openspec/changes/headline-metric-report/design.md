# Design: M0 headline metric report

## Context

BOD-311 delivered `compute_run_metrics` and `verdict metrics`. Issue #1213
asks for the M0 headline metric, accepted work per % of paid quota, with
per-role splits, frontier tokens, first-attempt pass rates, and a daily report
command. This change delivers the receipt-based part: accepted tasks per paid
model share, a proxy for the paid-quota denominator (see Denominator). The
paid-quota denominator itself is not delivered here.

## Goals / Non-Goals

**Goals:**
- Add per-role splits and frontier-token counts to the metrics surface.
- Add first-attempt pass rate by role and by cost class.
- Compute "accepted tasks / paid model share" from receipts (always available).
- Exclude fixture runs from the live aggregate; say so explicitly.
- Report each headline figure that cannot be computed as JSON null with a
  reason, never a string and never a zero fill.

**Non-Goals:**
- The paid-quota denominator (% of a provider's subscription window consumed).
  It needs the gateway's real quota endpoint; follow-up story.
- Planner/reviewer individual call token tracking (not in receipts; task filed).
- Research-duplication share (not in receipts; `research_metrics.py` has the
  per-call tool; a task is filed to wire it in).
- Daily timer service (follow-on task; deliver the command first).

## Denominator

"% of paid quota" can mean two things:
1. The fraction of model attempts on paid routes (from receipts).
2. The fraction of a provider's subscription window consumed (from OmniRoute).

This change computes meaning 1 only. `paid_model_share` is a proxy for the
paid-quota denominator, not the denominator itself: it says how much of the
work ran on paid routes, not how much of any provider's quota that work used.
Meaning 2 is not computed and the report makes no network call.

For the follow-up story: Verdict's own gateway reader takes quota from
`/api/resilience/connections`, which needs the management token
(`OMNIROUTE_MANAGEMENT_TOKEN`), and reads `remaining` and `limit` per
connection. The response shape must still be confirmed against the gateway docs
at `http://localhost:20128/docs` before any code relies on it.

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
  A node's first model attempt counts as a pass only when that attempt's
  `outcome` is `success` and the node's `final_state` is `VALIDATED` (Decision 5).
- Notes for planner tokens and research duplication.

### `verdict/headline_metric.py`

- `load_receipts_since(runs_dir, since)`: loads, verifies, and filters. A
  `runs_dir` that is not a directory returns an empty result. For each run it
  reads `receipt.json` and `events.jsonl` once with `O_NOFOLLOW`
  (`_read_bytes_nofollow`), applies the fixture and date filters to the parsed
  receipt, then calls `verify_run_receipt_from_bytes` on those same bytes; the
  dict it returns is the one parsed from the verified bytes.
- `aggregate_extended_metrics(receipts)`: aggregates `extend_run_metrics` output.
- `compute_headline_report(runs_dir, since)`: top-level entry point. Also
  returns `runs_dir` (the directory read) and `runs_dir_found` (whether it is a
  directory), so an empty report for a missing directory says why.
  Returns `headline_accepted_per_paid_share: float | null` and
  `headline_accepted_per_paid_share_note: str | null`; the note explains
  why the value is unavailable when the denominator is zero or absent.
- `format_headline_report(data)`: terminal output.

### `verdict/actions/registry.py` — `_action_headline_metric`

Thin wrapper around `compute_headline_report`; registered as `headline.metric`.
A missing `runs_dir` is an empty report, not an error. The only error it still
returns is an invalid `--since` date (exit 2).

### `verdict/orchestration/cli.py` — `_metrics` extension

`--daily` and `--since DATE` flags route to the headline action instead of the
per-run table when either flag is set. The positional `path` is resolved with
`_resolve_run`, as the per-run branch does: an existing directory is read as
given, anything else is a run id under `--runs-dir`, and the result is passed to
the action as `runs_dir`. It wins over `--runs-dir`. A path that resolves to no
directory prints `metrics path is not a directory: <resolved>` to stderr and
exits 2 before any report is computed. This is deliberately different from the
no-runs-directory case: a path the operator typed that does not exist is a
mistake to reject, while the default `.verdict/runs` not existing yet is a
fresh install.

## Test Strategy

- `test_extend_run_metrics_*`: in-memory receipt dicts; no fixture files needed.
- `test_fixture_runs_excluded`: verifies `load_receipts_since` skips `test-*`.
- `test_aggregate_extended_metrics_empty`: empty input.
- `test_compute_headline_report_*`: monkeypatches `load_receipts_since`.
- `test_action_headline_metric`: uses existing `tests/fixtures/metrics/`.
- #1315: first-attempt pass is tested through the real `DagRuntime` (fake
  executor, real `EventLog`, `build_run_receipt`, `extend_run_metrics`), for a
  worker that returns ok then fails validation and for one whose validation
  never finishes. `verdict metrics` is tested through the real argparse
  definitions with two distinct runs directories. The same-bytes test changes
  `receipt.json` on disk between the first read and any later read.
- No live calls in tests.

## Decisions

**Decision 1: receipt-based paid share as the M0 denominator.**
`paid_model_share` (subscription + metered attempts / total) is always
computable from local receipts without network access and without a management
token.  The paid-quota denominator is not part of this change (see Denominator).

**Decision 4: an unavailable number is null with a reason, not a sentinel string.**
`headline_accepted_per_paid_share`, `tasks_per_dollar` and `dollars` are `null`
(Python `None`) in the JSON output when they cannot be computed, with a
companion `*_note` key carrying the reason (no paid attempts, unknown cost, no
recorded spend, no runs). An empty report, a report with only free attempts and
a report with unknown cost are told apart by that reason, not by guessing from
the null. The same applies to every `per_run` row, which carries all three
headline figures. Zero is reported only when it was observed: counts over zero
runs are 0, but measured figures (`dollars`, `frontier_tokens`,
`first_attempt_pass_rate`) are `null` when nothing was observed, so an empty
report has null tokens and null pass rates, while a run whose receipt records
only free attempts observed 0 frontier tokens and reports 0. Secondary figures
(`frontier_tokens`, `first_attempt_pass_rate`) carry no note (for a pass rate,
null means no eligible first attempt for that role or cost class); they are
never a string or a zero fill. Callers check for missing data with a null guard instead of a
string parse. `compute_run_metrics` still returns the string `"undefined"` for
the existing per-run `verdict metrics` table, whose tests pin it; the headline
report normalises that value to null in `verdict/headline_metric.py` so its JSON
only ever carries a number or null, in the aggregate and in every `per_run` row.

**Decision 5 (#1315): a first attempt is a pass only when its work was validated.**
The attempt row's `outcome` comes from the worker's `terminal` event (`ok`),
which the runtime emits before blocked-output and validation checks. That alone
does not make the worker's exit a pass: when `_validate` (or the blocked-output
check) rejects the attempt, the runtime emits a `failure` event with the same
attempt number and the receipt sets that row to `failure`. What the row cannot
say is that validation never finished (for example the node driver crashed
after the terminal): the row stays `success` and the node ends `BLOCKED` or
`TERMINAL_SUCCESS`. `extend_run_metrics` therefore requires both
`outcome == "success"` and the node's `final_state == "VALIDATED"`. It is done
there and not in the receipt builder because adding a per-attempt field to
the receipt would stop every committed receipt from verifying. The
rule changes no other figure: `accepted_tasks` already required a COMPLETE run.

**Decision 6 (#1315): a missing runs directory is an empty report.**
The runs directory not existing is the normal state of a fresh install, and the
acceptance criteria always promised an empty report there. The action returns it
with exit 0, `runs_dir_found: false` and the usual null headline with the reason
"no runs in selected period". A mistyped `--runs-dir` therefore also gives an
empty report, which is why the report names the directory it read. A mistyped
positional path is different and is rejected (see the `_metrics` section). The
per-run `verdict metrics` table (no `--daily`/`--since`) keeps its own
"runs directory not found" error; it is not part of this change.

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

- The `test-` prefix convention for fixture exclusion is by agreement, not a
  signed property. Any live run whose `run_id` starts with `test-` is silently
  excluded. The report says how many were excluded so the operator sees if this
  fires unexpectedly.
- A mistyped `--runs-dir` gives an empty report with exit 0, not an error
  (Decision 6). The report says "Runs directory not found: <dir>" and carries
  `runs_dir_found: false`; scripts that need to tell the two apart read that
  field.
- `paid_model_share` is 0 when all runs are free (no subscription or metered
  attempts).  The report returns `null` for the headline and says so explicitly;
  no division by zero occurs.

## Security / trust implications

No new credentials, secrets or external writes. All inputs are local receipt
files already written by Verdict's own orchestration path; no external network
calls are made. No receipt contents are written or transmitted. The action
returns a plain dict; no credentials appear in the output.

## Routing / context / memory implications

No change to routing, admission, or eligibility logic. `extend_run_metrics`
reads an already-verified receipt dict and performs no I/O. `load_receipts_since`
reads the existing `receipt.json` and `events.jsonl` files once and calls the
existing `verify_run_receipt_from_bytes` on those bytes; it never writes (the
verifier's rebuild also reads `graph.json` and `review.json`, as the runs API
does). No context pack or memory store is touched.

## Migration / rollback

Additive. All existing `verdict metrics` behaviour (per-run table, `--json`)
is unchanged. `--daily` and `--since` route to the new action only when
explicitly passed; no existing call site is affected. Rollback is reverting
this branch: receipts already on disk are unaffected. The #1315 corrections
change only the headline report: a first-attempt pass rate can be lower than the
merged version reported, and a positional path is now honoured.

## ADR impact

None. The headline metric report is a read-only observability tool. No admission
gate, proof authority, or routing boundary changes. ADR-036 (goal-to-receipt),
ADR-049 (cost policy), and ADR-050 (Hindsight memory) are unaffected.
