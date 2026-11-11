# Proposal: keep project documentation fresh without manual help

## Why

`verdict doctor` fails its documentation preflight once the project documents pass their 24 h
life, and live acceptance fails with it. Nothing schedules a refresh: the 43 `verdict-core-docs`
documents were all ingested together, so they all expire together, and only a manual
`verdict doctor --fix` renews them (BOD-345 gap 2). A timer alone would not help: `--fix` skips
documents that are still fresh, so a six-hourly run would renew nothing until the instant they
expire and doctor would still be red for up to six hours a day.

## What Changes

- `run_documentation_preflight(fix=True)` also re-ingests a manifest that is still fresh but
  expires within `renew_margin_seconds` (default 12 h). Such documents count as `renewed`, never
  as `stale`. The read-only check (`fix=False`, which doctor uses) ignores the margin: an expired
  or missing document stays red.
- `verdict memory docs --install-service [--dry-run] [--uninstall]` installs
  `verdict-docs-refresh.service` and `.timer`: `verdict memory docs --fix` for the installing
  checkout, every six hours, with the same systemd bounds as the reference-docs timer.
- `verdict setup` plans it as host prerequisite `host.docs-refresh-timer`; `verdict doctor` shows a
  row for the timer.
- `ETHOS.md` NOW line updated with the memory ticket state (operator wording, BOD-339).

## Impact

- Changed: `verdict/documentation_preflight.py`, `verdict/setup_host.py`,
  `verdict/doctor_diagnostics.py`, `verdict/cli.py`, `verdict/commands/parsers_runtime.py`,
  `verdict/actions/{extra,registry}.py`, `docs/CLI_REFERENCE.md`, `ETHOS.md`.
- New: `verdict/docs_refresh_service.py`, `tests/test_docs_refresh_service.py`.
- No model calls, no network for a local checkout, no new dependency. Writes only the local
  shared memory database.
- Installing the timer on a host needs a separate operator OK; this change only adds it.

## Evidence boundary

KNOWN: all 43 documents of the 2026-11-11 fresh run share one `fresh_until`; `--fix` skips
manifests that pass `_manifest_is_fresh`. NOT AVAILABLE until installed: a measured 48 h run of
doctor and acceptance with the timer active.
