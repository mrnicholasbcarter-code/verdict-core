## Implementation

- [x] Write tests before implementation: fixture-based, no live calls, VERDICT_HOME in temp dir.
- [x] Add `verdict/discovery_funnel.py` with read-only `build_funnel` and `run_funnel` builder and `render_plain` renderer.
- [x] Add `verdict/commands/parsers_discovery.py` and wire `parsers_discovery.register(subparsers)` as a direct call in `verdict/cli.py`.
- [x] Add dispatch branch `elif args.command == "discovery"` in `verdict/commands/dispatch.py`.
- [x] Add `schemas/discovery-funnel.v1.json` and verify `--json` output validates against it.
- [x] Add OpenSpec change `openspec/changes/discovery-funnel-report` with spec and tasks.
- [x] Run focused tests, ruff check/format, mypy --strict, check_reachability, check_lint_gates.
- [ ] (post-merge) Run `verdict discovery funnel` against the live gateway with real `~/.verdict`, compare output with `docs/plan/20261119-model-discovery/baseline.yaml`, and record raw output in evidence.
