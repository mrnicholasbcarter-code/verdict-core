## Why

Operators who run `verdict orchestrate` have no programmatic way to list recent
runs, page through their events or verify a receipt without reading the filesystem
directly.  This change adds three read-only HTTP endpoints to `verdict serve` so
that tooling (dashboards, CI, the TUI) can query run state without file-system
access.

## What Changes

Three new read-only endpoints on the existing `verdict serve` FastAPI app:

- `GET /v1/runs` — list recent runs with run_id, started_at, outcome and
  receipt_status.  Bounded to a configured runs directory; never creates or
  modifies any file.
- `GET /v1/runs/{run_id}/events` — paginate `events.jsonl` for one run,
  filterable by node_id, type and sequence range.  Bounded response (1 MiB)
  with a continuation cursor.
- `GET /v1/runs/{run_id}/receipt` — return the stored `receipt.json` together
  with the result of `verify_run_receipt` (digest re-check, no writes).

All three share the same bearer-token authentication as the existing endpoints.
Run ids are validated against a strict alphanumeric pattern before any path is
resolved; the resolved path is confirmed to stay inside the configured runs root.

## Capabilities

### New Capabilities
- `run-trace-api`: Read-only HTTP trace API over orchestration run evidence.

### Modified Capabilities
<!-- none: existing receipt verifier, event log and orchestration modules unchanged -->

## Impact

New code: endpoints and helpers in `verdict/api.py`; schema `schemas/run-trace-api.v1.json`.
New tests: `tests/test_runs_api.py`.
New config: `VERDICT_RUNS_DIR` environment variable.
Touched docs: `docs/CONFIGURATION.md` (entry-points table, env-var table).
No existing behaviour changes.
