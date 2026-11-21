# ADR-048: UNKNOWN routes are free-tier until a probe charge proves otherwise

**Status:** Accepted (operator, 2026-11-21)
**Date:** 2026-11-21
**Deciders:** Operator
**Relates to:** ADR-0001 (control-plane invariants), ADR-043 (role-dependent
selection order), ADR-044 (escalation after judged failures),
`discovery-prober-registry` OpenSpec change,
`admission-reads-registry-proof` OpenSpec change

## Context

`classify_route_capacity` returns `CapacityClass.UNKNOWN` for any OmniRoute
route that has no declared pricing and no connection-level free signal.  At
the time this ADR was written, 2,135 routes fell into this class.

The existing agentic gate in `EligibilityLadder._task_gate` required
`capacity in {CapacityClass.FREE, CapacityClass.UNKNOWN}` to reach the gate
at all, but then the registry-proof branch (S3b) was gated on
`capacity == CapacityClass.FREE` only — UNKNOWN routes exited with
`no_agentic_probe` even when the discovery registry held a fresh `tool_ok=True`
entry.  The free-only discovery refresh (`verdict discovery refresh --free-only`)
also excluded UNKNOWN routes, so no registry entries were ever written for them.

The result: the entire UNKNOWN supply was inaccessible despite being potentially
free.  This directly undermined the D15 headline metric (accepted work per paid
dollar).

## Decision

Treat UNKNOWN capacity as free-tier for admission, unless and until a probe
observation proves it costs money.  Specifically:

1. **Discovery refresh**: `_PROBEABLE` and the `--free-only` filter both include
   `"unknown"`.  UNKNOWN routes are probed alongside FREE routes.

2. **Admission (tool_ok_proof)**: `entry.capacity_class in {"free", "unknown"}`
   qualifies via the registry proof; subscription and metered do not.

3. **Admission (EligibilityLadder)**: the registry-proof branch in `_task_gate`
   checks `capacity in {CapacityClass.FREE, CapacityClass.UNKNOWN}` (not just
   FREE).

4. **Charge detection**: after a liveness probe returns UP, if the
   `x-omniroute-response-cost` response header is non-zero, the route is written
   to `$VERDICT_HOME/discovery/metered.json` with evidence
   `"discovery_observed_charge"`.  `_capacity_class` checks this file and
   returns `CapacityClass.METERED`; the route is never probed or admitted again.

5. **Classification unchanged**: `classify_route_capacity` still returns UNKNOWN
   for routes without free evidence.  UNKNOWN is not promoted to FREE; it is
   admitted on the same terms as FREE, retaining the distinction in capacity
   ordering (FREE = tier 0, UNKNOWN = tier 3) so a known-free route is always
   preferred over an unconfirmed one.

## Consequences

- 2,135 UNKNOWN routes become eligible for discovery probing, expanding the
  provable free-model supply.
- Any route that charges per-request is permanently excluded after one probe,
  keeping paid spend at zero.
- The classification function is unchanged; existing tests for
  `test_route_name_never_makes_a_route_free` continue to pass (UNKNOWN is not
  FREE from name alone).
- `CAPACITY_ORDER[UNKNOWN] = 3` means a proven-free route is always ranked
  below a known-free one, so quality ordering is preserved.
