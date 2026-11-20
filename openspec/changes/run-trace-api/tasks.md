# Tasks

## 1. Implementation and focused verification

Ownership: story branch owner; verify with the focused tests named below.

- [x] 1.1 Add `GET /v1/runs`, `GET /v1/runs/{run_id}/events` and
      `GET /v1/runs/{run_id}/receipt` to `verdict/api.py`.
- [x] 1.2 Add `VERDICT_RUNS_DIR` with default `verdict_home()/"runs"`; document
      in `docs/CONFIGURATION.md`.
- [x] 1.3 Validate `run_id` against `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`;
      confirm resolved path is a direct child of the runs root.
- [x] 1.4 Add `schemas/run-trace-api.v1.json`.
- [x] 1.5 Add `tests/test_runs_api.py` with fixture run dirs, traversal tests,
      auth tests, pagination and receipt verification tests.
- [x] 1.6 Run focused tests, ruff check/format, mypy --strict,
      check_reachability, check_lint_gates.
- [x] 1.7 PR opened for independent review and release-train integration.
- [ ] (post-merge) Run `verdict serve` with real `~/.verdict` and exercise all
      three endpoints; record raw output in evidence.
