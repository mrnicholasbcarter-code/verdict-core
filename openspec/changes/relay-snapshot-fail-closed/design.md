# Design

## Context

`_offload_free_tier` in `verdict/intelligence.py` loads the OmniRoute
admission snapshot and uses it to build the admitted candidate set. When
snapshot loading fails — timeout, transport error, or stale last-good —
`_load_admit_snapshot` returns `(None, endpoint, True, error_str)`. The
function then reaches the guard at the bottom: before this change, it
returned `None` unconditionally. The `IntelligenceService.route` caller
then fell through to the historical catalog. The catalog carries no
admission proofs. The relay endpoint's proof-gate then denied the request
with the misleading "missing route-bound admission proof" error.

The relay path sets `_dispatch_gateway_origin` in the routing context (see
`verdict/api.py`). Offline callers — the CLI `Gate`, compatibility callers
— do not set it. The distinction is load-bearing: relay callers must never
silently fall to the historical catalog; offline callers should retain the
existing graceful fallback.

## Goals / Non-Goals

Goal: when the OmniRoute snapshot is unavailable on the relay dispatch
path, return a clear explicit denial with `fail_closed_snapshot_unavailable`
in `safety_flags` and a human-readable reason.

Non-goal: change the behaviour for callers without `_dispatch_gateway_origin`
in context. The existing `None` return and historical-catalog fallback are
intentional for those callers.

Non-goal: change when `_load_admit_snapshot` retries or how it ages out
the LKG. The fix acts at the point where `_offload_free_tier` decides what
to do with a `None` snapshot for relay callers.

## Decisions

Guard on `context.get("_dispatch_gateway_origin")` immediately before the
existing `return None` at the bottom of `_offload_free_tier`. When set
and `snapshot is None`, return a `RoutingDecision` with:
- `model = NO_ELIGIBLE_TARGET`
- `decision = "denied"`
- `safety_flags = ["fail_closed_snapshot_unavailable"]`
- reason includes `"fail_closed"` and the fetch error string if available

This mirrors `fail_closed_omniroute_unauthorized`, the existing pattern
for auth failures, and keeps the new code path narrow and local to
`_offload_free_tier`.

## Risks / Trade-offs

A `None` return from `_offload_free_tier` falls to the historical catalog,
which is a valid graceful degradation for offline callers. Narrowing the
guard to `_dispatch_gateway_origin` avoids breaking those callers. The
risk is that any new relay-path code that does not set
`_dispatch_gateway_origin` would miss the fail-closed guard; this is
acceptable because all relay-path routing goes through `api.py`, which
already sets the key.

The returned decision is explicit and logged; a future operator can grep
for `fail_closed_snapshot_unavailable` to find snapshot-unavailability
events.

### Security / trust implications

None. The change only affects whether routing returns an explicit denial
or falls to the catalog. No secrets, credentials or trust boundaries are
involved.

### Routing / context / memory implications

Relay callers receive a clear HTTP 503-class denial when the OmniRoute
snapshot is unavailable. The denial is explicit: `decision="denied"`,
`safety_flags=["fail_closed_snapshot_unavailable"]`, and a reason string
that does not mention "admission proof". Offline and CLI callers are
unaffected and continue to fall to the historical catalog.

### Migration / rollback

Revert the commit. No data migration; no stored state is involved. The
fix is a single conditional block in `_offload_free_tier`; removing it
restores the original `return None` behaviour.

### ADR impact

None. The change is consistent with ADR-0001 (control-plane invariants):
a dispatching caller that cannot get admission proof is denied, not silently
degraded.
