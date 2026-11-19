# Tasks

## 1. Implementation

- [x] 1.1 Add timeout-only retry and a timeout hint to `http_gateway_probe` / `authenticated_gateway_probe`
- [x] 1.2 Doctor binds a 10 s `/models` timeout (`VERDICT_DOCTOR_GATEWAY_TIMEOUT_S`) and a 3 s `/api/health` timeout (`VERDICT_DOCTOR_HEALTH_TIMEOUT_S`), each retried once on timeout only
- [x] 1.3 Red-first tests in `tests/test_doctor_gateway_probe_timeout.py` against a local stub server (failing assertions on origin/main, green after)
- [x] 1.4 Document both settings in `docs/CONFIGURATION.md`
- [x] 1.5 Focused tests, ruff check, ruff format --check, mypy --strict, doc links, reachability ratchet
- [x] 1.6 Local `verdict doctor --json` from the worktree against the real gateway

## 2. Live checks

- [ ] (post-merge) 2.1 From installed merged main, run `verdict doctor --json` against the live gateway under load and record that the gateway check passes and `gateway_lifecycle.state` is ready
