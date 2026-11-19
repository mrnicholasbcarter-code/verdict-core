# cli-output-truth: what the CLI prints must not mislead

## Why

The v1 launch workflow check (gaps G5, G6) and issue #1138 found three places where Verdict's
CLI output said something other than the truth:

- `verdict run-receipt` on a receipt whose `events.jsonl` changed exited 1, but its first line was
  still the recorded outcome, for example `COMPLETE: ... review PASS`. `--json` reported
  `"outcome": "COMPLETE"`.
- `verdict route --allow-offline --json` printed the human card before the JSON, so stdout was not
  valid JSON, and it exited 1 after returning a decision. The status spinner could also write to
  stdout in `--json` mode (follow-up `json-output-spinner-leak`).
- `verdict orchestrate --plain` showed the coloured live view when stdout was a pseudo-terminal.

## What Changes

- `run-receipt`: on failed integrity the first line is `INTEGRITY FAILED: <problems>`, then
  `recorded (unverified): <outcome>: <reason>`. `--json` gains `verified`; on failure `outcome` is
  `INTEGRITY_FAILED` and the recorded values move to `recorded_outcome` and `recorded_reason`.
  Verified receipts print as before (plus `"verified": true` in JSON). Exit codes are unchanged.
- `route --json`: stdout is exactly one JSON document and no spinner runs. On the gate path the
  document is the decision; exit 0 when a decision is returned (`--allow-offline` reports
  `"executed": false`), exit 1 with an `error` field when the decision is denied or a send failed.
- `orchestrate --plain`: the progress view uses a console that is never a terminal, so it always
  prints the ASCII narrative. `VERDICT_PLAIN=1` already selects it.

## Impact

- Code: `verdict/orchestration/cli.py` (`_receipt`, `_follow_console`, `_orchestrate`),
  `verdict/cli.py` (`cmd_route`, `_print_route_decision_json`).
- Tests: `tests/test_cli_output_truth.py`; the `run_receipt__fixture` JSON golden and the run-receipt wiring baselines are updated for
  the new integrity fields.
- Docs: `docs/CLI_REFERENCE.md`.
