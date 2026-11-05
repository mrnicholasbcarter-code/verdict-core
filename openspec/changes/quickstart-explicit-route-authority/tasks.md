## Implementation and verification

- [x] Replace the legacy route envelope with explicit operator-owned request input and existing bearer authentication (`scripts/quickstart.py`).
- [x] Exercise actual startup, API validation, execution authority and fail-closed behavior in isolated offline tests (`tests/test_quickstart_route_probe.py`; parent replay from the worktree root with temporary HOME/VERDICT_HOME and blocked sockets: 71 passed).
- [x] Independently review and rerun acceptance on the exact patch (second independent source review at `b8f75c1d` found no authority-boundary defect; parent re-ran the 71 tests, strict OpenSpec validation, Ruff and diff-check on the final diff).

Installed-main operational evidence (G7.1 on the installed runtime, the real
gateway and bearer token) is post-merge parent acceptance, not a ticked task.
This task list records local, offline verification only: it is not a merged-main,
installed-host acceptance or Done claim.
