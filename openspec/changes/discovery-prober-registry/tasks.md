## Implementation

- [x] Write tests before implementation: fixture-based, no live calls, VERDICT_HOME in temp dir.
- [x] Add `verdict/discovery/model.py` with Route, ProbeResult, Score types and status constants.
- [x] Add `verdict/discovery/probe.py` with injectable transport, probe_one, probe_all.
- [x] Add `verdict/discovery/toolprobe.py` with filler, classify, tool_probe, tool_probe_all.
- [x] Add `verdict/discovery/scores.py` with ScoreIndex, load_scores, parse_openrouter, parse_fcm.
- [x] Add `verdict/discovery/registry.py` with RegistryEntry, DiscoveryRegistry, load_registry, save_registry, _Breaker, run_refresh.
- [x] Add `verdict/discovery/refresh.py` with load_probeable_routes and run_refresh_action.
- [x] Add `verdict/discovery/omniroute_source.py` with load_omniroute_classified.
- [x] Add `verdict/discovery/__init__.py` exporting the public API.
- [x] Add `refresh` subparser in `verdict/commands/parsers_discovery.py`.
- [x] Add dispatch branch for `discovery refresh` in `verdict/commands/dispatch.py`.
- [x] Add OpenSpec change `openspec/changes/discovery-prober-registry`.
- [x] Run focused tests (70 pass), ruff check/format, mypy --strict on verdict/discovery/.
- [ ] (post-merge) Run `verdict discovery refresh --dry-run` against the live gateway with real `~/.verdict` and record raw output in evidence.
- [ ] (post-merge) Run `verdict discovery refresh` with `VERDICT_LIVE_SMOKE=1` against the live gateway and verify registry written to `~/.verdict/discovery/registry.json`.
