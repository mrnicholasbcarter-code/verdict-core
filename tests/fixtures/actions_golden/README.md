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

## Skipped commands and reasons

| Command | Reason |
|---------|--------|
| `doctor` | ~478KB output embedding filesystem paths and timing data; not deterministic |
| `catalog` | Output contains `captured_at`/`fresh_until` timestamps from live catalog |
| `eligibility` | Requires live OmniRoute network connection |
| `config` | No `config` command on `origin/main` (new in PR) |
| `receipt show` | Error cases output to stderr; valid receipt needs same fixture+path normalization as run-receipt |
| `compare` | No `--json` flag on `origin/main` |
| `stats` / `suggest` / `cost-report` | No `--json` flag on `origin/main` CLI |
| `route` | No `--json` flag on `origin/main` CLI |

## Normalised volatile fields

- `run_receipt__fixture`: Receipt content is from a static fixture file; no wall-clock fields.
  The run_dir path is passed as a parameter and normalized in the test.
- `credentials_list`: `OMNIROUTE_BASE_URL` always shows `set (len=18)` because the test
  environment sets `OMNIROUTE_BASE_URL=http://127.0.0.1:9` (exactly 18 chars).

## inputs/

- `run-receipt-run/receipt.json`: Minimal orchestration run receipt (BLOCKED outcome, no events.jsonl).
  Sourced from `tests/fixtures/live4_calibration/receipt.json` on `origin/main`.
