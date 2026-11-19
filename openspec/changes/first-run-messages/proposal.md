# Proposal: clear first-run messages for serve, doctor and orchestrate

## Why

A new user's first commands failed without telling them what to do (GitHub #1135, launch
gaps G2 and G4): `verdict serve` died with a Python traceback when `LLMGATE_AUTH_TOKEN` or
`VERDICT_RECEIPTS_DB` was unset; `verdict doctor` showed 12 FAILED rows and 0 OK on a fresh
install; `verdict orchestrate` on an empty state ended BLOCKED with `none eligible` and no next
step.

## What Changes

- `verdict serve` still refuses to start without auth or a receipts path, but prints one line
  naming the setting and the fix, and exits 2. No default receipts path is assumed (ADR-017).
- `verdict doctor`, before setup has written `verdict.yaml`, marks parts that do not exist yet
  as `not-configured` (never ok, still exit 1) and prints a first line pointing to `verdict setup`.
  Rows for parts that exist but are broken stay `failed`.
- An orchestrate BLOCKED reason caused by missing agentic proof says so in plain words and
  names the bounded `verdict identify --screen` command. Admission is unchanged.
- docs/CONFIGURATION.md documents both serve settings and a First run section.

## Impact

`verdict/api.py`, `verdict/commands/dispatch.py`, `verdict/serve_service.py`,
`verdict/doctor_diagnostics.py`, `verdict/cli.py`, `verdict/terminal_ui.py`,
`verdict/orchestration/{explain,run,runtime}.py`, docs/CONFIGURATION.md.
