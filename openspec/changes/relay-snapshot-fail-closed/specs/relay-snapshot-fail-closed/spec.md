# Spec Delta

## Purpose

When the OmniRoute admission snapshot is unavailable on the relay dispatch
path, the router SHALL return a clear explicit denial with
`fail_closed_snapshot_unavailable` in `safety_flags` rather than falling
to the historical catalog, which carries no admission proofs and produces
a misleading "missing route-bound admission proof" error.

## ADDED Requirements

### Requirement: Relay path fails closed on snapshot unavailability

When `_offload_free_tier` reaches the snapshot-unavailable path AND the
routing context has `_dispatch_gateway_origin` set (relay dispatch), it
SHALL return a denied `RoutingDecision` with `decision="denied"` and
`"fail_closed_snapshot_unavailable"` in `safety_flags`.

#### Scenario: Relay dispatch denied on snapshot unavailability

- **WHEN** the OmniRoute snapshot loader raises `LiveAdmitError("timeout", …)`
  AND the routing context contains `_dispatch_gateway_origin`
- **THEN** the routing decision has `model == "no_eligible_target"`,
  `decision == "denied"`, `"fail_closed" in reason`, and
  `"fail_closed_snapshot_unavailable" in safety_flags`; the reason does
  NOT contain "missing route-bound admission proof"

#### Scenario: Non-relay dispatch uses historical path

- **WHEN** the OmniRoute snapshot loader raises `LiveAdmitError("timeout", …)`
  AND the routing context does NOT contain `_dispatch_gateway_origin`
- **THEN** the routing decision does NOT have
  `"fail_closed_snapshot_unavailable"` in `safety_flags` (historical
  catalog fallback is used)
