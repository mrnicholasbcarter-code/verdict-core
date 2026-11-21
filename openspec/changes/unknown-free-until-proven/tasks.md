# Tasks

## 1. Discovery: probe UNKNOWN routes

- [x] 1.1 Add `"unknown"` to `_PROBEABLE` in `verdict/discovery/refresh.py`;
      verify with `pytest tests/test_u1_unknown_free.py -k unknown_probed`
- [x] 1.2 `--free-only` filter includes `cc in {"free", "unknown"}`; verify
      with `pytest tests/test_u1_unknown_free.py -k free_only`
- [x] 1.3 Metered-override routes excluded before probing in
      `run_refresh_action`; verify with
      `pytest tests/test_u1_unknown_free.py -k subscription_and_metered`

## 2. Charge detection: permanent METERED override

- [x] 2.1 Add `observed_cost: float | None = None` to `ProbeResult` in
      `verdict/discovery/model.py`
- [x] 2.2 `probe_one` reads `x-omniroute-response-cost` header on UP responses
      and stores it in `ProbeResult.observed_cost`
- [x] 2.3 Add `verdict/discovery/metered.py` with `MeteredEntry`,
      `load_metered_overrides`, `add_metered_override`
      (atomic write, `$VERDICT_HOME/discovery/metered.json`)
- [x] 2.4 `run_refresh` detects non-zero `observed_cost` on UP probe, calls
      `add_metered_override`, skips tool probe for that route with
      `tool_skip_reason="metered_override"`; verify with
      `pytest tests/test_u1_unknown_free.py -k charged_probe`

## 3. Admission: accept UNKNOWN on same terms as FREE

- [x] 3.1 `tool_ok_proof` in `verdict/discovery/registry.py` accepts
      `entry.capacity_class in {"free", "unknown"}`
- [x] 3.2 `EligibilityLadder._task_gate` registry-proof branch checks
      `capacity in {CapacityClass.FREE, CapacityClass.UNKNOWN}` (not just FREE)
- [x] 3.3 `EligibilityLadder._capacity_class` checks metered overrides file and
      returns `CapacityClass.METERED` with evidence `"discovery_observed_charge"`
      when the route is in `metered.json`; verify with
      `pytest tests/test_u1_unknown_free.py -k registry_proof`
- [x] 3.4 Update `discovery_funnel.py` `registry_proof_count` to include
      `capacity_class == "unknown"` entries

## 4. Docs, invariants, decision log

- [x] 4.1 Add D31 to `docs/decisions/LOG.md`
- [x] 4.2 Add `docs/adr/ADR-048-unknown-free-until-proven.md`
- [x] 4.3 Add ADR-048 row to `docs/adr/README.md`
- [x] 4.4 Add invariant row to `AGENTS.md` citing new tests

## 5. Tests

- [x] 5.1 `tests/test_u1_unknown_free.py` with 5 tests:
      `test_unknown_probed_in_free_only`,
      `test_subscription_and_metered_not_probed_free_only`,
      `test_charged_probe_becomes_metered_excluded`,
      `test_unknown_registry_proof_admitted`,
      `test_route_name_alone_never_free`
