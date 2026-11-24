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
  verified receipts (excluding fixture runs), optionally enriches with live
  OmniRoute quota data, and computes the headline metric. `format_headline_report`
  renders it for the terminal.
- `verdict/actions/registry.py`: New action `headline.metric` calls
  `compute_headline_report`.
- `verdict/orchestration/cli.py`: Adds `--daily` and `--since DATE` flags
  to `verdict metrics`. When `--daily` or `--since` is set, the
  headline metric report is shown instead of the per-run table.
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

## Impact

- No breaking changes. All existing `verdict metrics` flags still work.
- `extend_run_metrics` is additive: it calls and embeds `compute_run_metrics`.
- Fixture runs (run_id starting with `test-`) are excluded from the live
  aggregate and counted separately.
- The daily timer is a follow-on task tracked in `tasks.md`.

## Acceptance Criteria

- `verdict metrics --daily` prints the headline metric from real receipts (or
  says "0 runs processed" on a fresh install).
- Fixture runs are excluded and counted separately in the output.
- `verdict metrics --daily --json` returns a machine-readable dict.
- `extend_run_metrics` returns per-role splits, frontier tokens, and
  first-attempt pass rate from fixture receipts.
- Research-duplication note appears in output, explaining it is not tracked.
- M0 baseline recorded from the operator's machine (post-merge task).

## Evidence Boundary

- KNOWN: `compute_run_metrics`, `verify_run_receipt`, the receipt schema,
  `NodeKind` values, the `test-` fixture prefix convention, and OmniRoute quota
  API shape were read from this repository and are covered by the tests added
  here.
- INFERRED: how many real receipts exist on the operator's machine and whether
  OmniRoute quota tracking is enabled for any connection; both are measured at
  run time, never assumed.
- NOT AVAILABLE: a live end-to-end `verdict metrics --daily` run on the
  operator's machine. This is the M0 baseline recorded as a post-merge task.
