# Tasks

## 1. Core types and prober

- [x] 1.1 Add `verdict/discovery/model.py` with `Route`, `ProbeResult`, `Score`
      types and status constants; verify with
      `pytest tests/test_discovery_prober.py -k model`
- [x] 1.2 Add `verdict/discovery/probe.py` with injectable transport and
      `probe_one`; `probe_all` removed — registry uses per-pool serial probing;
      verify with `pytest tests/test_discovery_prober.py`
- [x] 1.3 Add `verdict/discovery/toolprobe.py` with `filler`, `classify`,
      `tool_probe`; `tool_probe_all` removed — registry uses per-pool serial
      tool probing; verify with `pytest tests/test_discovery_toolprobe.py`
- [x] 1.4 Add `verdict/discovery/scores.py` with `ScoreIndex`, `load_scores`,
      `parse_openrouter`, `parse_fcm`; verify with
      `pytest tests/test_discovery_scores.py`

## 2. Registry

- [x] 2.1 Add `verdict/discovery/registry.py` with `RegistryEntry` (including
      `probe_skip_reason` distinguishing not-probed from probed-but-down),
      `DiscoveryRegistry`, `load_registry`, `save_registry`, `_Breaker`,
      `run_refresh`; verify with `pytest tests/test_discovery_registry.py`
- [x] 2.2 Per-pool circuit breaker opens on 429; subsequent routes in that pool
      get `probe_skip_reason="pool_breaker_open"`; verify with
      `pytest tests/test_discovery_registry.py -k breaker`
- [x] 2.3 Tool-probe stage uses `ThreadPoolExecutor` across pools and serial
      probing within each pool, mirroring the liveness stage; verify with
      `pytest tests/test_discovery_registry.py -k tool`

## 3. Refresh action and OmniRoute source

- [x] 3.1 Add `verdict/discovery/refresh.py` with `load_probeable_routes` and
      `run_refresh_action`; route source: OmniRoute via
      `fetch_omniroute_evidence` + S2's `rows_from_evidence` adapter; no
      sources-cache.json input (no producer exists for that file); verify with
      `pytest tests/test_discovery_prober.py::LoadOmniRouteRoutesTest`
- [x] 3.2 `--dry-run` returns `source="dry_run_requires_catalog"` immediately
      with zero network calls; the OmniRoute catalog requires a live fetch;
      verify with
      `pytest tests/test_discovery_registry.py -k dry_run`

## 4. CLI wiring

- [x] 4.1 Add `verdict/discovery/__init__.py` exporting the public API
      (including S2's `fetch_models`/`normalize_model_record` once, no
      duplicates); verify with `pytest tests/test_discovery_*.py`
- [x] 4.2 Add `refresh` subparser in `verdict/commands/parsers_discovery.py`;
      verify with `pytest tests/test_discovery_prober.py -k parsers`
- [x] 4.3 Add dispatch branch for `discovery refresh` in
      `verdict/commands/dispatch.py`; verify with
      `pytest tests/test_discovery_registry.py -k dispatch`

## 5. Quality gates

- [x] 5.1 Write tests before implementation: fixture-based, no live calls,
      `VERDICT_HOME` in temp dir.
- [x] 5.2 All 208 offline tests pass; ruff check/format and mypy --strict clean.
- [x] 5.3 Reachability clean with `--ratchet-base origin/main` (0 NEW).
- [x] 5.4 All lint gates pass and vgate PASS.
- [ ] (post-merge) Run `verdict discovery refresh --dry-run` against the live
      gateway with real `~/.verdict` and record raw output in evidence.
- [ ] (post-merge) Run `verdict discovery refresh` with `VERDICT_LIVE_SMOKE=1`
      against the live gateway and verify registry written to
      `~/.verdict/discovery/registry.json`.
