# Tasks

## 1. Implementation

- [x] 1.1 `verdict/discovery/refresh.py` — add `free_only` parameter to `run_refresh_action`
- [x] 1.2 `verdict/commands/parsers_discovery.py` — add `--free-only` flag to `refresh` subcommand
- [x] 1.3 `verdict/commands/dispatch.py` — forward `free_only` to `run_refresh_action`
- [x] 1.4 `verdict/actions/registry.py` — add `free_only` to `_known` and forward to `run_refresh_action`
- [x] 1.5 `verdict/discovery_refresh_service.py` — service manager (verdict-discovery-refresh.{service,timer})
- [x] 1.6 `verdict/setup_host.py` — `DISCOVERY_REFRESH` action (host.discovery-refresh-timer)
- [x] 1.7 `verdict/doctor_diagnostics.py` — `_discovery_refresh_timer_row` and `_discovery_refresh_freshness_row`
- [x] 1.8 `tests/test_discovery_refresh_service.py` — 21 unit tests; behavioural proof of free_only filtering
- [x] 1.9 Run focused tests, ruff check, ruff format --check, mypy --strict, check_lint_gates; push with vgate
- [ ] (post-merge) 1.10 CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 Install the timer with `verdict setup --apply --allow host.discovery-refresh-timer` and confirm `$VERDICT_HOME/discovery/registry.json` stays younger than `REGISTRY_TTL_S` (12 h) with no manual step; record raw output
- [ ] (post-merge) 2.2 `verdict doctor` shows "Discovery registry refresh timer: ok" and "Discovery registry freshness: ok" after install; record raw output
