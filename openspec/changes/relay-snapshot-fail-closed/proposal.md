# Proposal: relay snapshot fail-closed

## Why

When the hourly acceptance job or a live relay request arrives and the
OmniRoute admission snapshot cannot be fetched (timeout, stale last-good,
network error), `_offload_free_tier` returned `None`. The caller then fell
through to the historical catalog, which carries no admission proofs. The
relay endpoint's proof-gate then raised HTTP 503 "dispatch denied: missing
route-bound admission proof" — a misleading error that looked like a
routing misconfiguration rather than a temporary snapshot unavailability.

The 16:00Z acceptance run on 2026-11-20 triggered this: a fresh serve
process had no in-process LKG, the on-disk LKG was 57 minutes old (beyond
the 15-minute `ADMIT_LAST_GOOD_MAX_AGE_SECONDS` window), and the OmniRoute
admit surfaces timed out. The result was a spurious 503 that failed the
acceptance gate with a confusing error message.

## What Changes

- `verdict/intelligence.py::_offload_free_tier`: when `_fetch_snapshot`
  returns `None` (no live snapshot and no usable LKG) and the routing
  context has `_dispatch_gateway_origin` set (the relay dispatch path),
  return a denied `RoutingDecision` with `decision="denied"`,
  `safety_flags=["fail_closed_snapshot_unavailable"]`, and a clear reason
  string instead of returning `None`. Returning `None` falls to the
  historical catalog, which has no admission proofs and produces the
  misleading proof-gate error.
- Callers without `_dispatch_gateway_origin` in context (offline use,
  CLI gate, compatibility callers) continue to use the historical path.

## Capabilities

### New Capabilities

- `relay-snapshot-fail-closed`: when the OmniRoute admission snapshot is
  unavailable on the relay dispatch path, the router returns a clear denied
  decision with `fail_closed_snapshot_unavailable` in `safety_flags`,
  rather than falling to the historical catalog and producing a misleading
  "missing route-bound admission proof" error.

### Modified Capabilities

None. The relay endpoint's proof-gate behaviour is unchanged; it now
receives an explicit denial upstream instead of the catalog fallback.

## Impact

`verdict/intelligence.py` and `tests/test_authoritative_eligibility.py`.
No schema changes, no dependency changes, no new environment variables.

## Acceptance criteria

1. With a monkeypatched `load_omniroute_admit_snapshot` that raises
   `LiveAdmitError("timeout", …)` and context `{"_dispatch_gateway_origin":
   "http://127.0.0.1:20128/v1"}`, the router returns a decision with
   `model == "no_eligible_target"`, `decision == "denied"`,
   `"fail_closed" in reason`, and `"fail_closed_snapshot_unavailable" in
   safety_flags`. The reason must NOT contain "missing route-bound
   admission proof".
2. Same monkeypatch without `_dispatch_gateway_origin` in context returns
   a decision without `fail_closed_snapshot_unavailable` in `safety_flags`
   (historical path still used for offline callers).

## Evidence boundary

- KNOWN: `_offload_free_tier` returned `None` when `_fetch_snapshot`
  returned `(None, …)` with no LKG available, regardless of caller type.
- KNOWN: the relay endpoint (`api.py`) sets `_dispatch_gateway_origin`
  in the routing context before calling `service.route()`.
- INFERRED: the `fail_closed_omniroute_unauthorized` pattern (existing,
  for auth failures) confirms the approach; this change mirrors it for
  snapshot-unavailable failures.
- NOT AVAILABLE: a live acceptance run after the fix; the regression tests
  exercise the full routing stack with a monkeypatched snapshot loader.
