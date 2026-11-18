# Design: BOD-311 efficiency metrics from receipts

## Context

Receipts are written by `write_run_receipt` in `verdict/orchestration/receipt.py`. Each receipt contains:
- `nodes`: list of nodes with `attempts`, each attempt has `capacity_class`, `route_id`, `outcome`, `usage.cost_usd`
- `review`: `status`, `blocking`, `route_id`
- `integration`: `ok`, `barriers`, `missing`
- `outcome`: `COMPLETE`/`BLOCKED`/etc from `completion_verdict`
- `events_digest`: SHA-256 of `events.jsonl`

Verification is done by `verify_run_receipt` which recomputes the receipt from source files and compares.

## Goals / Non-Goals

**Goals:**
- Single command `verdict metrics <path> [--json]` accepting a run directory or runs directory root.
- Reuse existing verification; refuse tampered receipts.
- Compute four numbers per acceptance criteria.
- Output human-readable text and JSON.

**Non-Goals:**
- Live provider calls.
- Historical trend analysis.
- Cost projection or forecasting.

## Risks / Trade-offs

- Receipts may have missing `usage` or `cost_usd` fields (older schema). Treat as unknown.
- A run may have multiple implement/integrate nodes; each validated node counts as one accepted task.
- Free share denominator: all model attempts (capacity_class in {free, subscription, metered, unknown}), not just successful ones.
- Subscription share reported separately for transparency.

## Code Structure

### New Action: `verdict.actions.registry._action_metrics`

```python
def _action_metrics(runs_dir: str, run_id: str | None = None, json_output: bool = False) -> ActionResult:
    # If run_id: single run. Else: scan runs_dir for receipt.json files.
    # For each: verify_run_receipt, completion_verdict.
    # Aggregate across verified COMPLETE runs.
```

### CLI Integration

In `verdict/orchestration/cli.py`:
- Add `metrics` subparser with `path` (run dir or runs dir), `--runs-dir`, `--json`.
- Dispatch to `_metrics` function calling `run_action("metrics", ...)`.

### Computation Logic

```python
def compute_metrics(receipt: dict) -> dict:
    # 1. Accepted tasks: count of nodes where
    #    - kind in {implement, integrate}
    #    - final_state == VALIDATED
    #    - receipt.integration.ok == True
    #    - receipt.review.status == PASS and review.blocking == 0
    #    (This matches completion_verdict logic)
    
    # 2. Dollars: sum of attempt.usage.cost_usd for all attempts where cost_usd is not None
    #    Track has_unknown_cost = any attempt has no usage or usage.cost_usd is None
    
    # 3. Tasks per dollar: accepted / dollars if dollars > 0 and not has_unknown_cost else "undefined"
    
    # 4. Free share: free_attempts / total_model_attempts
    #    Subscription share: sub_attempts / total_model_attempts
    #    Model attempt = attempt where route_id not empty and not local/ and route_identity != mechanical
```

### Test Strategy

- Fixture receipts in `tests/fixtures/metrics/`:
  - `complete-run/`: COMPLETE run with known costs, mixed capacity classes.
  - `tampered-run/`: receipt.json modified after events.jsonl (digest mismatch).
  - `unknown-cost-run/`: COMPLETE run with some attempts missing cost_usd.
  - `blocked-run/`: BLOCKED run (not counted).
- Tests in `tests/test_orch_metrics.py`:
  - Single run metrics.
  - Directory aggregation.
  - Tampered receipt refused.
  - Unknown cost handling.
  - Free/subscription share calculation.

## Decisions

- Use `compute_run_metrics` function in `verdict/orchestration/receipt.py` to keep metrics computation close to receipt structure.
- Reuse `verify_run_receipt` and `completion_verdict` for verification — no new verification logic.
- Tampered receipts are skipped with a clear error message, not counted in aggregates.
- Unknown costs propagate to aggregate level: if any run has unknown cost, total dollars is unknown.
- Free share denominator includes all model attempts (not just successful), matching the acceptance criteria.
- Output format: human-readable by default, JSON with `--json` flag.

## Security / trust implications

- Receipts are digest-verified (SHA-256 of `events.jsonl`) before metrics are computed. Tampered receipts are refused.
- No live provider calls; all computation is local and deterministic.
- Cost data comes from `usage.cost_usd` recorded in receipts, which originates from executor-reported usage. No external billing APIs are called.
- No secrets are exposed in metrics output; only aggregate numbers and capacity class shares.

## Routing / context / memory implications

- No changes to routing, admission, or eligibility logic.
- No changes to context packing or memory systems.
- Metrics command reads existing receipts; does not write to memory or emit events.
- No impact on worker context or planner.

## Migration / rollback

- New command only; no migration required for existing runs.
- Rollback: remove `metrics` subparser from CLI and `_action_metrics` from registry.
- No database or schema changes.

## ADR impact

- No new ADRs required. This change adds a read-only analytical command on top of existing receipt infrastructure.
- Consistent with ADR-036 (goal-to-receipt orchestration): receipts are the authoritative evidence; metrics derive from them.
- Consistent with receipt verification invariants (tampering detected via `events_digest`).