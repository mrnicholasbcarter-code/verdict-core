# Proposal: autodev pinned METERED executor allowed, selection sweeps exclude METERED

## Why

`verdict autodev --allow-live --executor-model <route>` probed any pinned route
unconditionally. A fact-proven METERED (pay-per-token) route would spend real
money on the probe itself, before any task was dispatched. The selection sweep
path (`prepare_live_autodev_selected`) already excluded METERED routes, but the
pinned path (`prepare_live_autodev`) had no corresponding guard. The safety fix
adds a capacity filter to both paths.

The companion policy update (operator ruling 2026-11-23, Linear BOD-342 D32
/ ADR-049): a METERED route pinned by the operator via `--executor-model` is an
explicit operator choice. It is allowed, probed, and its capacity class is
recorded in the report and receipt so the spend is visible. The selection sweep
guard is unchanged — METERED routes are never auto-selected.

KNOWN: the selection sweep (`build_selector` capacity filter) already blocked
METERED routes from admission before this change. KNOWN: the pinned probe had no
corresponding capacity-class check at all. KNOWN: `EligibilityLadder.capacity_fact`
returns a class from declared or observed billing facts without issuing a probe.
INFERRED: an empty `price` dict for METERED candidates is accepted by
`public_execution_path_request`; the cost ledger records `status="unknown"` for
per-token terms, which is honest since probe responses do not carry per-token
rates. NOT AVAILABLE until a live run: whether METERED pinned routes appear in
real operator use and whether the receipt's `pinned_route_capacity_class` field
is surfaced correctly in the TUI.

## What Changes

- **`verdict/orchestration/eligibility.py`**: Added `capacity_fact(route_id)`
  method to `EligibilityLadder`. Returns `(CapacityClass, str)` using declared
  or observed billing facts, without issuing a probe.

- **`verdict/orchestration/eligibility_report.py`**: `build_selector` capacity
  filter now uses a no-probe `EligibilityLadder` classifier for capacity-class
  narrowing. UNKNOWN routes pass through (ADR-049 `assume_free`); only
  fact-proven METERED routes are narrowed out of selection sweeps.

- **`verdict/autodev_run.py`** (`prepare_live_autodev`):
  - Removed the fail-closed block that raised `AutodevError` for METERED pinned
    routes; METERED pinned routes are now allowed (operator ruling 2026-11-23).
  - Zero-cost probe check is bypassed when the pinned route is METERED.
  - `headers["x-verdict-route-capacity"] = cls.value` is added to the returned
    headers after the probe succeeds; `probe_headers` in the receipt candidate
    is snapshotted before this key so the receipt stores only real gateway
    facts.
  - `is_free: not metered_pinned` and `"price": {}` are set on the candidate
    dict for METERED routes.

- **`verdict/autodev_run.py`** (`run_autodev`): Added `pinned_capacity_class:
  str = ""` parameter; written to `record_payload` as
  `pinned_route_capacity_class` when non-empty.

- **`verdict/autodev_run.py`** (`AutodevReport`): Added
  `pinned_route_capacity_class: str = ""` field, surfaced in `to_dict()` and
  `summary()`.

- **`verdict/cli.py`** (`cmd_autodev`): Extracts `x-verdict-route-capacity`
  from probe headers after `prepare_live_autodev` and passes it as
  `pinned_capacity_class` to `run_autodev`.

- **Unchanged**: `_live_planner_models` still excludes METERED routes from
  planner selection; the `--orchestrator-model` guard in `cmd_autodev` still
  fails closed for METERED. Only the pinned executor probe path is relaxed.

## Acceptance criteria

- Fact-METERED routes are narrowed from selection sweeps before any probe is issued (test).
- UNKNOWN routes pass the capacity filter (test).
- A METERED pinned executor is probed; `x-verdict-route-capacity: metered` appears in the returned headers (test).
- `pinned_route_capacity_class: "metered"` appears in the receipt and survives `redact_sensitive_dict` (test).
- Explicit `--orchestrator-model` that is METERED is refused with a named error including `capacity=metered` (test).
- On merged main, `verdict autodev --allow-live --executor-model <metered-route> --objective "echo ok" --json` records `pinned_route_capacity_class: "metered"` in the JSON output and the receipt.

## Capabilities

### New Capabilities

- `autodev-pinned-metered-executor`: behaviour of a METERED route pinned by
  `--executor-model`: allowed, probed, `pinned_route_capacity_class` recorded
  in report and receipt.

### Modified Capabilities

- `autodev-live-planner-selection`: the capacity filter used by `build_selector`
  for selection sweeps now explicitly excludes METERED and passes UNKNOWN.

## Impact

`verdict/orchestration/eligibility.py` (new method), `verdict/orchestration/
eligibility_report.py` (capacity narrowing classifier),
`verdict/autodev_run.py` (probe path, run_autodev, AutodevReport),
`verdict/cli.py` (extract and pass capacity class),
`tests/test_autodev_wiring.py` (97 tests, 3 new).

No change to the eligibility ladder, the health cache, the planner path, the
orchestrator-model guard, the receipt digest, or the admission invariants.

CP1 (#1244) is the owner of the full tiered fallback ladder (free → subscription
→ paid) and the spend-limit policy. This change does not build an automatic paid
fallback; if no cheaper candidate exists for the selection sweep, the existing
fail-closed behaviour is unchanged.
