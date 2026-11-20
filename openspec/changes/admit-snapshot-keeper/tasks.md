# Tasks

## 1. Implementation

- [x] 1.1 `verdict/admit_snapshot_service.py` — service manager (verdict-admit-snapshot.{service,timer})
- [x] 1.2 `verdict/commands/parsers_admission.py` — `verdict admission refresh` CLI parser
- [x] 1.3 `verdict/commands/dispatch.py` — `_cmd_admission` and `_cmd_admission_refresh`
- [x] 1.4 `verdict/cli.py` — register parsers_admission
- [x] 1.5 `verdict/setup_host.py` — `ADMIT_SNAPSHOT` action (host.admit-snapshot-timer)
- [x] 1.6 `verdict/doctor_diagnostics.py` — `_admit_snapshot_timer_row` and `_admit_snapshot_freshness_row`
- [x] 1.7 `verdict/actions/registry.py` — `admission.refresh` action registered
- [x] 1.8 `tests/test_admit_snapshot_service.py` — 15 unit tests; fake transport; proves no inference endpoint called
- [x] 1.9 Run focused tests, ruff check, ruff format --check, mypy --strict, check_lint_gates; push with vgate
- [ ] (post-merge) 1.10 CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 Install the timer with `verdict setup --non-interactive --apply --allow host.admit-snapshot-timer` and confirm `~/.verdict/admit-last-good.json` stays younger than 15 min for 2 h with no manual step; record raw output
- [ ] (post-merge) 2.2 The hourly acceptance `serve` check is green for 3 consecutive runs after the timer is installed; record raw output
