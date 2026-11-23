# Design

## Context

`verdict autodev --allow-live` has two execution paths:

- **Pinned** (`prepare_live_autodev`): the operator names a specific route with
  `--executor-model`. Before this change, there was no capacity check; any route
  was probed, including fact-METERED (pay-per-token) routes.
- **Unpinned** (`prepare_live_autodev_selected`): the system selects from the
  eligibility ladder, which already excluded METERED routes via a capacity
  filter in `build_selector`.

The safety fix closes the gap in the pinned path. The policy update (operator
ruling 2026-11-23, BOD-342 D32 / ADR-049) states that a METERED route pinned
by the operator is an explicit choice: allow it, record the capacity class in
the report and receipt so spend is visible. CP1 (#1244) owns the spend-limit
and the full tiered fallback ladder.

## Goals / Non-Goals

**Goals:**
- Selection sweeps (`build_selector`) exclude fact-METERED routes; UNKNOWN
  routes pass through (ADR-049 `assume_free`).
- A METERED route pinned with `--executor-model` is allowed and probed. The
  zero-cost check is bypassed. `pinned_route_capacity_class` is recorded in
  the receipt and in `AutodevReport`.
- Planner and orchestrator-model paths are unchanged (METERED stays excluded).

**Non-Goals:**
- Automatic METERED fallback when no cheaper candidate exists. That is CP1.
- Spend-limit enforcement. That is CP1.
- Full tiered fallback ladder (free → subscription → paid). That is CP1.

## Decisions

1. **`capacity_fact` on `EligibilityLadder`** — returns `(CapacityClass, str)`
   without probing. Reading billing facts from connection state is cheap and
   needed in both paths (the pinned path to record the class, and the selection
   sweep classifier). Single method on the existing class; no new interface.

2. **No-probe capacity classifier in `build_selector`** — a bare `EligibilityLadder`
   with a `lambda _: HealthResult(False, "not_probed")` health callable is
   constructed for the capacity filter. It reads facts only, never probes. The
   `AdmissionStage.POLICY` narrowing with `source="orchestrate:capacity"` matches
   the existing admission-narrowing contract.

3. **Allow METERED pinned, skip zero-cost** — the fail-closed block is removed.
   The zero-cost check `float(cost) != 0` is guarded by `not metered_pinned`
   so it still applies to free and subscription routes.

4. **`"price": {}`** — `public_execution_path_request` requires a `price` dict
   for non-free candidates. METERED per-token rates live in route metadata, not
   in probe response headers, so an empty dict is passed. The cost ledger records
   `status="unknown"` for per-token terms (graceful degradation defined in
   `tokens_to_usd_term`). The `pinned_route_capacity_class` field is the
   spend-visibility signal, not a dollar estimate.

5. **`probe_headers` snapshot** — the synthetic `x-verdict-route-capacity` key
   is added to the returned `headers` dict after snapshotting `probe_headers =
   dict(headers)`. This keeps the `probe_headers` stored in the receipt candidate
   pure (only real gateway `x-omniroute-*` facts). The returned `headers` (and
   `route_probe_headers` in JSON output) carries the synthetic key for the caller.

6. **`AutodevReport.pinned_route_capacity_class`** — the class is surfaced in
   `to_dict()` (JSON output) and `summary()` (plain-text output) so both paths
   show the spend signal without digging into the receipt.

## Risks / Trade-offs

- An operator who accidentally passes a METERED `--executor-model` will now
  spend money on the probe and on every unit. The capacity class is visible in
  the report, but there is no pre-flight warning. CP1 adds the spend limit.
- The `"price": {}` empty dict means per-token cost terms in the receipt have
  `status="unknown"`. This is honest but means cost reporting is incomplete
  for METERED pinned runs until CP1 wires in the route metadata price.
- The no-probe capacity classifier constructs a full `EligibilityLadder` per
  `build_selector` call. This is consistent with how the ladder is already
  constructed in that code path and is not a hot path.

## Security / trust implications

- A METERED pinned route is an explicit operator choice. The route still passes
  the same probe (content, identity, gateway headers) as any other route; only
  the zero-cost assertion is relaxed. The admission ladder, invariants, and
  receipt digest are unchanged.
- `pinned_route_capacity_class` is a Verdict-derived field (not operator input),
  written from `CapacityClass.value` after `capacity_fact` reads billing facts.
  It is not user-controlled and cannot be forged by the route being probed.
- No new credentials, secrets, or provider keys are introduced.

## Routing / context / memory implications

- The no-probe capacity classifier in `build_selector` reads billing facts from
  connection state; it does not add new network calls to the selection sweep.
- `AutodevReport.pinned_route_capacity_class` is an in-memory field for the
  current run; it is not persisted across runs or added to any memory store.

## Migration / rollback

- No data migration. Existing receipts without `pinned_route_capacity_class` are
  unaffected (the field is absent when the pinned route is not METERED).
- Roll back by restoring the fail-closed block and the zero-cost assertion for
  all routes, removing the `probe_headers` snapshot, and removing the
  `pinned_route_capacity_class` field from `AutodevReport` and `run_autodev`.

## ADR impact

- ADR-049 (cost policy and unknown handling, 2026-11-21): `assume_free` for
  UNKNOWN routes is enforced by the capacity filter. METERED pinned executor
  is the first production use of the "PAID only from facts" direction that
  ADR-049 anticipates for CP1.
- ADR-001 (control plane invariants): admission narrowing via POLICY stage
  still holds; the capacity filter only removes METERED routes, never re-admits.
