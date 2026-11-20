# Tasks

## 1. Registry proof function

- [x] 1.1 Add `_canonical_route_key(route_id)` to `verdict/discovery/registry.py`
      that strips the `omniroute/` gateway prefix; verify with
      `pytest tests/test_s3b_registry_admission.py -k prefix`
- [x] 1.2 Add pure function `tool_ok_proof(route_id, registry, now)` to
      `verdict/discovery/registry.py`; returns `{"qualified": bool, "reason":
      str|None, "source": "discovery_registry", "entry": RegistryEntry|None}`;
      checks all 7 conditions (capacity=free, answered, tool_ok, served_model,
      TTL); never raises; verify with
      `pytest tests/test_s3b_registry_admission.py`
- [x] 1.3 Export `tool_ok_proof` from `verdict/discovery/__init__.py`

## 2. EligibilityLadder agentic gate

- [x] 2.1 Add `_discovery_registry_loaded: bool` and `_discovery_registry_cache`
      lazy-load cache to `EligibilityLadder.__init__`; registry is loaded at
      most once per `evaluate()` call sequence
- [x] 2.2 Before returning `no_agentic_probe` for a FREE route, call
      `tool_ok_proof` with the lazy-loaded registry; fall through to admission
      when qualified; fall back to `no_agentic_probe` when not qualified;
      UNKNOWN routes bypass this path entirely

## 3. Funnel report

- [x] 3.1 Add optional `registry: Any | None = None` parameter to
      `build_funnel()`; compute `registry_proof_count` from entries with
      `capacity_class == "free"` and a qualifying `tool_ok_proof`; attach as
      `registry_proof` key in the `tool_ok` stage when non-None
- [x] 3.2 Update `run_funnel()` to load the registry via `load_registry()` and
      pass it to `build_funnel()`
- [x] 3.3 Update `render_plain()` to show `via registry proof: N` after the
      `tool_ok` drops line when the count is present

## 4. Tests

- [x] 4.1 Write `tests/test_s3b_registry_admission.py` covering: fresh proof
      admits, expired does not, tool_ok=None/False/answered=False do not,
      PRICED/UNKNOWN not promoted, gateway prefix stripped, absent/corrupt
      registry no exception, funnel registry_proof count

## 5. Quality gates

- [x] 5.1 All 196 offline tests pass (185 existing + 11 new S3b tests);
      `uv run ruff check . && uv run ruff format --check .` clean;
      `uv run mypy verdict --strict` clean
- [x] 5.2 Reachability clean: `tool_ok_proof` has a production caller in both
      `EligibilityLadder.evaluate()` and `build_funnel()`;
      `check_reachability.py --ratchet-base origin/main` reports 0 NEW
- [x] 5.3 All lint gates pass: `scripts/check_lint_gates.py`
- [ ] (post-merge) Run `verdict discovery funnel` against the live setup after
      `verdict discovery refresh` has populated the registry; verify
      `via registry proof: N` line appears with N > 0
- [ ] (post-merge) Run `verdict route` for a FREE route that previously showed
      `no_agentic_probe`; verify it is now ranked
