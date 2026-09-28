# tests/fixtures/actions_golden

Golden fixtures for `tests/test_actions_json_golden.py`.

Each `.json` file captures the exact `stdout`, `exit_code`, and `stderr_empty`
flag from `origin/main` for the corresponding CLI command run under a hermetic
environment (no API keys, isolated HOME/XDG, `OMNIROUTE_BASE_URL=http://127.0.0.1:9`,
`NO_COLOR=1`, `CI=1`).

The test asserts that `run_action()` on the PR branch produces `data` equal to
the captured JSON when parsed.

## Files

| Fixture | Command | Action name | Notes |
|---------|---------|-------------|-------|
| `models__default.json` | `models --json` | `models.list` | Stable static catalog |
| `plan__default.json` | `plan --json` | `setup.plan` | Deterministic offline plan |
| `probe__refused.json` | `probe model-a --json` | `probe` | Exit 2; no live consent |
| `detect__offline.json` | `detect --offline --json` | `detect` | No network; stable |
| `credentials_list__default.json` | `credentials list --json` | `credentials.list` | OMNIROUTE_BASE_URL always `set (len=18)` in test env |
| `replay__missing.json` | `replay missing-session --json` | `replay` | Exit 1; session not found |
| `run_receipt__fixture.json` | `run-receipt <run_dir> --json` | `run-receipt` | Uses `inputs/run-receipt-run/receipt.json`; exit 1 (BLOCKED, missing events.jsonl) |
| `stats__no_log.json` | `stats` | `stats` | No `--json` flag; human stdout; no log file → deterministic warning |
| `cost_report__no_log.json` | `cost-report` | `cost-report` | No `--json` flag; human stdout; no log file → deterministic warning |
| `suggest__no_log.json` | `suggest` | `suggest` | No `--json` flag; human stdout; no log file → "no suggestions" |
| `route__terse_offline.json` | `route "write a hello world" --allow-offline --terse` | `route` | No `--json` flag; `--terse` emits JSON error payload; exit 1 (offline transport=error) |
| `route__default_offline.json` | `route "write a hello world" --allow-offline` | `route` | No `--json` flag; human table + JSON footer; Latency and timestamp normalised |
| `compare__offline.json` | `compare "write a hello world" --allow-offline` | `compare` | No `--json` flag; JSON comparison report; timestamp and latency normalised |
| `receipt_show__missing.json` | `receipt show nonexistent-id-f2test --json --db <path>` | `receipt show` | Exit 1; missing receipt → error on stderr, empty stdout |
| `catalog__refused.json` | `catalog --base-url http://127.0.0.1:9 --json` | `catalog` | Exit 1; connection refused → URLError JSON payload; fully deterministic (no timestamps) |
| `eligibility__faked_inventory.json` | `eligibility --json --no-pager` (via shim) | `eligibility` | Faked 2-row inventory via `tests/helpers/eligibility_golden_shim.py`; exit 0 |

## Skipped commands and reasons

| Command | Reason |
|---------|--------|
| `doctor --json` | ~500 KB output embedding host-specific filesystem paths (`/home/nick/…`) and a 867-doc documentation inventory listing; not reproducible across hosts |
| `config` | No `config` command on `origin/main` (new in PR) |

## Normalised volatile fields

- `route__default_offline`, `route__terse_offline`: `Latency  0.0ms` replaces
  wall-clock latency in the human table; `"timestamp": "NORMALIZED"` replaces
  the ISO-8601 `strategy_selection.timestamp` in the JSON footer.
- `compare__offline`: `"timestamp": "NORMALIZED"` and `"latency_ms": 0.0`
  replace the volatile comparison fields.
- `credentials_list`: `OMNIROUTE_BASE_URL` always shows `set (len=18)` because the test
  environment sets `OMNIROUTE_BASE_URL=http://127.0.0.1:9` (exactly 18 chars).
- `run_receipt__fixture`: Receipt content is from a static fixture file; no wall-clock fields.
  The run_dir path is passed as a parameter and normalized in the test.

## inputs/

- `run-receipt-run/receipt.json`: Minimal orchestration run receipt (BLOCKED outcome, no events.jsonl).
  Sourced from `tests/fixtures/live4_calibration/receipt.json` on `origin/main`.

## Capture helper

- `tests/helpers/eligibility_golden_shim.py`: Subprocess shim that monkeypatches
  `verdict.orchestration.run.fetch_inventory` and `fetch_connections` with a fixed
  2-row fixture before calling `verdict.cli.main(["eligibility", "--json", "--no-pager"])`.
  Run by `test_eligibility_faked_inventory_golden` to avoid live OmniRoute dependency.
