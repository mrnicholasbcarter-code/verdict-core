# BOD-304: Durable real-session failure feedback (first increment)

## Why

The existing HealthCache and SessionLedger serve different purposes. HealthCache
stores expiring route availability and scoped blockers. SessionLedger stores
controller-verified task outcomes. A real worker terminal availability failure
must reach the existing health cache without creating another evidence store or
turning task-quality failures into route-health failures.

## What Changes

Add a reusable `record_worker_failure` helper and additive schema-1 negative
feedback metadata. Runtime and planner callers project real failures through the
ladder hook; the ladder reads active negatives and scoped cooldowns before
selection and dispatch. Only adapter-attested live terminal failures qualify. This helper does not
record positive health, capability passes, task quality or review judgements.

Expiry follows the classified cooldown, capped at 24 hours. Route-local failures
stay route-local. Credential-scope failures use the selected canonical credential
pool identity, or a provider fallback when no pool is known. Rate/quota credential
failures also zero the same pool bucket for that bounded interval.

## Limits and validation

Focused offline pytest fixtures cover persistence, half-open expiry, scoped
cooldowns, independent writers, injected/scripted exclusion, and unknown turns.
These are bootstrap implementation checks. No authenticated live orchestration,
self-hosting proof, paid calls, savings claim or production health proof is implied.
Focused runtime/planner checks and cross-run selection regressions pass. Full
release-train validation and independent cross-family review remain required. Existing admission remains fail-closed; expiry is not positive proof.

## Capabilities

### Modified Capabilities

- orchestration: preserve explicit failure scope and durable execution evidence.

## Impact

Only orchestration failure handling and its focused tests/docs change. The model
transport, credentials, paid-spend controls, independent review and admission
membership rules are unchanged.

## Acceptance Criteria

- Focused production-path regressions prove the defined failure behavior.
- Task failures and injected fixtures never become live provider-health proof.
- Unexpected or retryable infrastructure failures retain bounded recovery.

## Evidence boundary

KNOWN: focused offline tests and touched-source checks pass on the story branch.
INFERRED: the corrected failure mechanism reduces unchanged repeat attempts.
NOT AVAILABLE: authenticated live self-hosted completion and cross-family
release approval; these remain explicit release blockers.
