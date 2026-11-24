# Proposal: M0 headline metric report

**Story #1213** — `[M0] Headline metric report: accepted work per % paid quota, daily`

## Why

The ETHOS mission metric is "accepted work per paid dollar, and per % of paid
quota". Without a command that computes it from verified receipts, every other
story is unmeasurable. This change adds the first version of that command so
the M0 baseline can be recorded.

## What Changes

- `verdict/orchestration/receipt.py`: Add `extend_run_metrics(receipt)` — builds
  on `compute_run_metrics` and adds per-role splits, frontier tokens, and
  first-attempt pass rate. Research-duplication share is not in receipts; the
  function documents this explicitly.
- `verdict/headline_metric.py`: New module. `compute_headline_report` aggregates
  verified receipts (excluding fixture runs) and computes accepted tasks per
  paid model share. It reads local files only and makes no network call.
  `format_headline_report` renders it for the terminal. Any number that cannot
  be computed is JSON null, never a string; the headline figures (`dollars`,
  `tasks_per_dollar`, `headline_accepted_per_paid_share`) also carry a reason.
- `verdict/actions/registry.py`: New action `headline.metric` calls
  `compute_headline_report`.
- `verdict/orchestration/cli.py`: Adds `--daily` and `--since DATE` flags
  to `verdict metrics`. When `--daily` or `--since` is set, the
  headline metric report is shown instead of the per-run table. The positional
  `path` is honoured (follow-up #1315, below).
- `tests/test_headline_metric.py`: Unit tests for `extend_run_metrics`,
  `compute_headline_report`, fixture exclusion, and the action.

## Capabilities

### New Capabilities

- `verdict metrics --daily` — M0 headline metric report across all verified runs.
- `verdict metrics --since YYYY-MM-DD` — filter to runs from a given date.
- `extend_run_metrics(receipt)` — per-role attempt/accepted splits, frontier
  tokens, first-attempt pass rate.

### Partial Delivery

This PR delivers accepted-tasks / paid-model-share (fraction of model attempts
on paid routes, computed from local receipts).  It does **not** deliver
accepted-tasks / % paid quota (the fraction of a provider's subscription window
consumed), which is what issue #1213 ultimately asks for.  The gateway quota
endpoint is `/api/resilience/connections` (management-token gated, different
field names from what a naive implementation assumes).  That denominator is
tracked in a follow-up story.  `Refs #1213` is used rather than `Closes #1213`.

### Follow-up (#1315): corrections found by the post-merge review

The first version had four defects. Corrected in the same change, before it is
archived:

- **First-attempt pass counts only validated work.** A node's first attempt is a
  pass only when the attempt succeeded and the node ended `VALIDATED`. A
  worker that returned ok and was then rejected by validation was already
  recorded as a failed attempt (the runtime emits a `failure` event for the same
  attempt after `_validate`; traced, no change needed). A worker that returned ok
  and whose validation never finished (the node driver crashed) kept a
  `success` attempt on a node that never validated and was counted as a pass.
  It no longer is.
- **The positional path is read.** `verdict metrics <path> --since DATE` (or
  `--daily`) reports on `<path>`, with the same resolution as the per-run table:
  an existing directory is read as given, anything else is a run id under
  `--runs-dir`. A path that resolves to no directory exits 2 with a clear error.
  `--runs-dir` is used when no path is given.
- **A fresh install is an empty report, exit 0.** The runs directory not
  existing gives `Runs processed:   0` and exit 0, which is what the acceptance
  criteria below always said; the merged action returned exit 2. The report
  carries `runs_dir` and `runs_dir_found`, and the text report says "Runs
  directory not found", so a mistyped `--runs-dir` is visible rather than
  silent.
- **Verification and aggregation read the same bytes.** `receipt.json` and
  `events.jsonl` are read once per run, without following symlinks, and the
  verified bytes are the aggregated bytes (`verify_run_receipt_from_bytes`).

## Impact

- No breaking changes to the flags: all existing `verdict metrics` flags still
  work. Behaviour changes from #1315: a positional path with `--daily` or
  `--since` is now read (it was ignored); a first-attempt pass rate can only go
  down or stay the same, never up, because a pass now needs a validated node;
  a symlinked `receipt.json` is skipped as a verification failure (as the runs
  API already does).
- `extend_run_metrics` is additive: it calls and embeds `compute_run_metrics`.
- Fixture runs (run_id starting with `test-`) are excluded from the live
  aggregate and counted separately.
- The daily timer is a follow-on task tracked in `tasks.md`.

## Acceptance Criteria

- `verdict metrics --daily` prints the headline metric from real receipts. On a
  fresh install, where the runs directory does not exist, it prints an empty
  report ("Runs processed:   0", "Runs directory not found: <dir>") and exits 0.
- `verdict metrics <dir> --since DATE` reports on `<dir>`, not on `--runs-dir`.
- A node whose first attempt returned ok but did not end `VALIDATED` is not a
  first-attempt pass.
- Fixture runs are excluded and counted separately in the output.
- `verdict metrics --daily --json` returns a machine-readable dict.
- `extend_run_metrics` returns per-role splits, frontier tokens, and
  first-attempt pass rate from fixture receipts.
- Research-duplication note appears in output, explaining it is not tracked.
- M0 baseline recorded from the operator's machine (post-merge task).

## Evidence Boundary

- KNOWN: `compute_run_metrics`, `verify_run_receipt_from_bytes`, the receipt
  schema, `NodeKind` values and the `test-` fixture prefix convention were read
  from this repository and are covered by the tests added here. The runtime
  emits a `failure` event for the same attempt after `_validate` rejects it
  (traced through `DagRuntime` with a real event log in `tests/test_headline_metric.py`).
- INFERRED: how many real receipts exist on the operator's machine; this is
  measured at run time, never assumed.
- NOT AVAILABLE: a live end-to-end `verdict metrics --daily` run on the
  operator's machine (the M0 baseline, a post-merge task), and the gateway's
  quota response shape. No code in this change depends on the gateway.
