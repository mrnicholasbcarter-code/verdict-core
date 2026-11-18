# Proposal: BOD-311 efficiency metrics from receipts

## Why

**KNOWN**: Operators need a single command that computes the four BOD-8 benchmark numbers from digest-verified receipts: accepted tasks, dollars spent, accepted tasks per dollar, and free share. No hand-entered values; tampered receipts are refused.

## What Changes

- Add a new `verdict metrics` command (or flag on existing command) that reads one or more run directories, verifies each receipt against its `events.jsonl` digest, and computes:
  1. **Accepted tasks**: implement/integrate nodes with `final_state=VALIDATED`, integration barriers ok, review PASS with 0 blocking.
  2. **Dollars**: sum of `cost_usd` across all model attempts; unknown cost (`null` or missing) reported as unknown, never 0.
  3. **Accepted tasks per dollar**: undefined when dollars is 0 or unknown; say so explicitly.
  4. **Free share**: fraction of model attempts on routes with `capacity_class=free`; subscription counted separately.
- Reuse `verify_run_receipt` and `completion_verdict` from `verdict.orchestration.receipt`.
- Fixture tests with tampered receipt and unknown-cost receipt; fixtures labelled as fixtures per AGENTS.md.

## Capabilities

### New Capabilities

- `efficiency-metrics`: Compute benchmark numbers from verified receipts.

## Impact

**KNOWN**: New CLI command in `verdict/orchestration/cli.py`, new action in `verdict/actions/registry.py`, focused tests. No eligibility, provider health, recovery, executor, gateway, registry or memory authority changes. No installs or live calls.

**INFERRED**: Operators can run `verdict metrics <runs-dir> --json` and get the four numbers immediately.

**NOT AVAILABLE**: No live proof in this change; fixture runs are labelled fixtures.

## Acceptance Criteria

- [ ] `verdict metrics <run-dir>` prints four numbers for a single run.
- [ ] `verdict metrics <runs-dir>` aggregates across all verified receipts in a directory.
- [ ] Tampered receipt is refused (not counted, error reported).
- [ ] Unknown cost is reported as unknown, never 0.
- [ ] Free share counts capacity_class=free; subscription counted separately.
- [ ] Focused tests pass; ruff, mypy, doc links, reachability, OpenSpec validators pass.
- [ ] Real-run output pasted from BOD-300 and recent runs.